"""Every MCP server a dispatch selects, product (`repo_mcp`) or profile (`--mcp`), under one
contract: an ambiguous name refuses, and an unsafe name or URL never reaches a diagnostic, a
permission event or telemetry. Matching keeps the raw names.

Configured through agent.yaml, the product clone's .mcp.json and a dispatch record `new` wrote;
the policy runs through the Codex hook, Claude's fallback decision and `build_settings`."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, telemetry
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_permissions
from agent_cli.hooks.paths import PROTECTED_NOTE
from dispatch_helpers import droot  # noqa: F401 -- pytest fixture, used only as a parameter name

pytestmark = pytest.mark.usefixtures("notes_remote")

URL = "https://helper.internal/v1/zq7suffix"
OTHER_URL = "https://elsewhere.internal/zq8suffix"
BLOCK = f"""
worker_mcp:
  helper:
    server: helper
    command: helper-server
    allow: [mcp__helper__*]
    deny: [mcp__helper__private]
  extra:
    server: extra
    command: extra-server
    allow: [mcp__extra__*]
    deny: [mcp__extra__drop]
  endpoint:
    server: "{URL}"
    command: endpoint-server
    deny: ["Bash(stat {URL}:*)"]
  unselected-url:
    server: plain
    command: plain-server
    deny: ["Bash(curl {OTHER_URL}:*)"]
  spaced:
    server: "q w"
    command: spaced-server
    deny: ["Bash(stat q w e:*)"]
  spaced-long:
    server: "q w e"
    command: spaced-server
  delim:
    server: alpha__b
    command: delim-server
    allow: [mcp__alpha__b__*]
  trail:
    server: alpha_
    command: trail-server
    allow: [mcp__alpha___*]
  near:
    server: alpha_b
    command: near-server
    allow: [mcp__alpha_b__*]
  broken-endpoint:
    server: "{URL}"
    command: endpoint-server
    deny: ["Bash(curl {URL}/:*)"]
  broken-spaced:
    server: "q w"
    command: spaced-server
    deny: ["Bash(stat q w/:*)"]
  broken-unselected:
    server: plain
    command: plain-server
    deny: ["Bash(curl {OTHER_URL}/:*)"]
  bare: {{server: bare, command: bare-server, allow: [mcp__bare]}}
  exact: {{server: bare, command: bare-server, allow: [mcp__bare__read, "Bash(stat x:*)"], deny: ["Bash(stat x y:*)"]}}
"""


@pytest.fixture
def sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sink"

    def sink_root(_root: Path) -> Path:
        return path

    monkeypatch.setattr(telemetry, "sink_root", sink_root)
    return path


def _events(sink: Path) -> list[dict[str, Any]]:
    files = sorted((sink / ".agent" / "telemetry" / "events").glob("*.jsonl"))
    lines = [line for f in files for line in f.read_text(encoding="utf-8").splitlines()]
    return [e for e in map(json.loads, lines) if e["kind"] == "hook.denial"]


def _configure(root: Path, repo_mcp: list[str]) -> None:
    from dispatch_helpers import _set_alpha_dispatch  # pyright: ignore[reportPrivateUsage]

    reg = root / "agent.yaml"
    reg.write_text(reg.read_text(encoding="utf-8") + BLOCK, encoding="utf-8")
    _set_alpha_dispatch(root, repo_mcp=repo_mcp, allowed_tools=["mcp__alpha__*", "mcp__helper__*"])
    defined = {name: {"command": "product-server"} for name in repo_mcp}
    clone = root / "repos/alpha-api"
    (clone / ".mcp.json").write_text(json.dumps({"mcpServers": defined}), encoding="utf-8")
    for git in (["add", ".mcp.json"], ["commit", "-qm", "product mcp"]):
        subprocess.run(["git", "-C", str(clone), *git], check=True, capture_output=True)


def _dispatch(root: Path, monkeypatch, mcp: list[str], repo_mcp: list[str] | None = None) -> dict:
    """A queued dispatch `new` recorded with `mcp` profiles and product server alpha selected."""
    from dispatch_helpers import _new_fast  # pyright: ignore[reportPrivateUsage]

    _configure(root, ["alpha"] if repo_mcp is None else repo_mcp)
    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda self, *a, **k: None})
    )
    d = _new_fast(root, watch=False, mcp=mcp, base="HEAD")
    assert dispatch.load(root, d["id"])["mcp"] == mcp
    return d


def _recorded(root: Path, d: dict, mcp: list[str]) -> dict:
    rec = dispatch.load(root, d["id"])
    rec["mcp"] = mcp
    dispatch.save(root, rec)
    return rec


def _codex(root: Path, d: dict, tool: str, **tool_input: str) -> tuple[str, str]:
    return codex_permissions.decision(root, d["id"], {"tool_name": tool, "tool_input": tool_input})


def _alpha(root: Path) -> repos_mod.Repo:
    return repos_mod.load_registry(root).require("alpha")


# ------------------------------------------------------------------ ambiguous selected names


@pytest.mark.parametrize(
    ("repo_mcp", "mcp", "name"),
    [
        (["alpha"], ["delim"], "alpha__b"),
        (["alpha"], ["trail"], "alpha_"),
        ([], ["delim"], "alpha__b"),
        ([], ["helper", "trail"], "alpha_"),
    ],
)
def test_a_profile_server_that_runs_into_the_delimiter_refuses_to_launch(
    droot: Path, monkeypatch, repo_mcp, mcp, name
):
    with pytest.raises(AgentError) as exc:
        _dispatch(droot, monkeypatch, mcp, repo_mcp)
    profile = mcp[-1]
    assert (
        f"dispatch.mcp profile {profile!r} selects {name!r}, whose name runs into the MCP rule delimiter"
        in exc.value.message
    ), exc.value.message
    assert "drop the profile from dispatch.mcp" in exc.value.message


def test_a_repo_selection_that_runs_into_the_delimiter_still_refuses(droot: Path):
    _configure(droot, ["alpha__b"])
    with pytest.raises(AgentError, match="repo_mcp selects 'alpha__b'.*drop it from repo_mcp"):
        dispatch.build_settings(droot, _alpha(droot))


@pytest.mark.parametrize("profile", ["delim", "trail"])
def test_a_recorded_ambiguous_profile_refuses_every_tool_on_both_providers(
    droot: Path, monkeypatch, capsys, profile
):
    d = _dispatch(droot, monkeypatch, ["near"])
    rec = _recorded(droot, d, [profile])
    for tool in ("mcp__alpha__read", "mcp__alpha__b__read", "mcp__alpha___read", "Bash"):
        with pytest.raises(AgentError, match=f"dispatch.mcp profile '{profile}' selects"):
            _codex(droot, d, tool, command="stat README.md")
    with pytest.raises(AgentError, match=f"dispatch.mcp profile '{profile}' selects"):
        dispatch.rules.decide("Bash", {"command": "stat README.md"}, rec, _alpha(droot), droot)
    monkeypatch.setenv("AGENT_ROOT", str(droot))
    monkeypatch.setenv("AGENT_DISPATCH_ID", d["id"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "mcp__alpha__b__read"})))
    capsys.readouterr()
    assert codex_permissions.main() == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_safe_prefixes_tool_suffixes_and_profile_denies_keep_working(droot: Path, monkeypatch):
    d = _dispatch(droot, monkeypatch, ["near", "helper", "extra"])
    verdicts = {
        "mcp__alpha_b__read": "allow",  # a safe name that only shares alpha's prefix
        "mcp__alpha__read__all": "allow",  # a tool suffix holding the delimiter
        "mcp__alpha__read": "allow",
        "mcp__helper__private": "deny",  # the profile's deny beats the repo's wildcard
        "mcp__helper__read": "allow",
        "mcp__extra__read": "allow",  # granted by the profile alone
        "mcp__extra__drop": "deny",  # the second profile's deny holds too
        "mcp__other__read": "deny",
    }
    assert {tool: _codex(droot, d, tool)[0] for tool in verdicts} == verdicts


def test_without_a_profile_the_repo_grants_are_unchanged(droot: Path, monkeypatch):
    d = _dispatch(droot, monkeypatch, [])
    assert _codex(droot, d, "mcp__helper__private")[0] == "allow"
    assert _codex(droot, d, "mcp__extra__read")[0] == "deny"
    assert _codex(droot, d, "Bash", command=f"stat {URL}")[0] == "allow"


# ------------------------------------------------------------------ rules in reasons and lint


@pytest.mark.parametrize(
    ("mcp", "command"),
    [
        (["endpoint"], f"stat {URL} -L"),  # a selected server's endpoint
        (["unselected-url"], f"curl {OTHER_URL}"),  # a URL no selection names
        (["spaced"], "stat q w e"),  # a selected name the label redacts, no URL
        (["spaced-long", "spaced"], "stat q w e"),  # overlapping names, both orders
        (["spaced", "spaced-long"], "stat q w e"),
    ],
)
def test_a_matched_unsafe_deny_is_withheld_but_still_denies(droot: Path, monkeypatch, mcp, command):
    d = _dispatch(droot, monkeypatch, mcp)
    rec = dispatch.load(droot, d["id"])
    assert _codex(droot, d, "Bash", command=command) == ("deny", "Denied by dispatch rule [redacted-rule]")
    behavior, reason = dispatch.rules.decide("Bash", {"command": command}, rec, _alpha(droot), droot)
    assert behavior == "deny" and reason.startswith("`[redacted-rule]` is never a worker's"), reason
    # A safe matched rule keeps its text.
    assert _codex(droot, d, "Bash", command="rm -rf build") == (
        "deny",
        "Denied by dispatch rule Bash(rm -r*)",
    )


@pytest.mark.parametrize("profile", ["broken-endpoint", "broken-spaced", "broken-unselected"])
def test_a_malformed_unsafe_profile_deny_refuses_without_printing_it(droot: Path, monkeypatch, profile):
    with pytest.raises(AgentError) as exc:
        _dispatch(droot, monkeypatch, [profile])
    message = exc.value.message
    assert (
        "alpha worker deny: [redacted-rule] stops mid-token, so it matches nothing. A prefix rule has to "
        "end on a whole token: write a prefix that ends on a whole token or spell the command out."
    ) in message, message
    for secret in ("helper.internal", "zq7suffix", "elsewhere.internal", "zq8suffix", "q w"):
        assert secret not in message, message


# ------------------------------------------------------------------ permission events and telemetry


def _hook(root: Path, d: dict, monkeypatch, tool: str, **tool_input: str) -> dict[str, Any]:
    monkeypatch.setenv("AGENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_DISPATCH_ID", d["id"])
    return codex_permissions.run_event({"tool_name": tool, "tool_input": tool_input})


def _without_ts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k != "ts"} for e in events]


def test_the_codex_hook_stores_and_logs_only_safe_identities(droot: Path, monkeypatch, sink: Path):
    d = _dispatch(droot, monkeypatch, ["endpoint", "extra"])
    unsafe = f"mcp__{URL}__read"
    assert _codex(droot, d, unsafe) == ("deny", "No dispatch MCP rule allows this tool")
    _hook(droot, d, monkeypatch, unsafe, command="ls /zq9arg")
    _hook(droot, d, monkeypatch, "mcp__extra__drop")
    _hook(droot, d, monkeypatch, "Bash", command=f"stat {URL}")
    _hook(droot, d, monkeypatch, "mcp__extra__read")
    omitted = {"input": "[tool arguments omitted]", "input_omitted": True}
    assert _without_ts(dispatch.load(droot, d["id"])["permission_events"]) == [
        {
            "tool": "[redacted-mcp-tool]",
            **omitted,
            "decision": "deny",
            "reason": "No dispatch MCP rule allows this tool",
        },
        {"tool": "mcp__extra__drop", **omitted, "decision": "deny", "reason": "Denied by dispatch MCP rule"},
        {"tool": "Bash", **omitted, "decision": "deny", "reason": "Denied by dispatch rule [redacted-rule]"},
        {
            "tool": "mcp__extra__read",
            **omitted,
            "decision": "allow",
            "reason": "Codex policy hook allowed this call.",
        },
    ]
    data = [e["data"] for e in _events(sink)]
    shown = [(e["command"], e["reason"]) for e in data]
    assert shown == [
        ("[redacted-mcp-tool]", "No dispatch MCP rule allows this tool"),
        ("[redacted-mcp-tool]", "No dispatch MCP rule allows this tool"),
        ("mcp__extra__drop", "Denied by dispatch MCP rule"),
        ("stat", "Denied by dispatch rule [redacted-rule]"),
    ]
    assert all(e["hook"] == "worker-policy" and e["class"] == "policy" for e in data)
    stored = json.dumps([dispatch.load(droot, d["id"]), _events(sink)])
    for secret in ("helper.internal", "zq7suffix", "zq9arg"):
        assert secret not in stored


def test_claudes_fallback_stores_and_logs_only_a_safe_mcp_identity(droot: Path, monkeypatch, sink: Path):
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

    d = _dispatch(droot, monkeypatch, ["endpoint"])
    rec = dispatch.load(droot, d["id"])
    callback = dispatch.supervisor.permission_callback(droot, rec, _alpha(droot))
    calls = [
        (f"mcp__{URL}__read", {"command": f"{URL} /zq9arg"}, PermissionResultDeny),
        ("mcp__other__read", {"command": "ls"}, PermissionResultDeny),
        ("Bash", {"command": f"stat {URL}"}, PermissionResultDeny),
        ("Bash", {"command": "stat README.md"}, PermissionResultAllow),
    ]
    for n, (tool, tool_input, result) in enumerate(calls):
        context = ToolPermissionContext(tool_use_id=f"toolu_{n}", agent_id="sub-1")
        assert isinstance(asyncio.run(callback(tool, tool_input, context)), result)
    denied = "Fall-through callback denied this call; detailed diagnostic returned to worker."
    fixed = {"input": "[tool arguments omitted]", "input_omitted": True, "agent_id": "sub-1"}
    assert _without_ts(dispatch.load(droot, d["id"])["permission_events"]) == [
        {
            "tool": "[redacted-mcp-tool]",
            **fixed,
            "tool_use_id": "toolu_0",
            "decision": "deny",
            "reason": denied,
        },
        {"tool": "mcp__other__read", **fixed, "tool_use_id": "toolu_1", "decision": "deny", "reason": denied},
        {"tool": "Bash", **fixed, "tool_use_id": "toolu_2", "decision": "deny", "reason": denied},
        {
            "tool": "Bash",
            **fixed,
            "tool_use_id": "toolu_3",
            "decision": "allow",
            "reason": "Fall-through callback allowed this call.",
        },
    ]
    data = [e["data"] for e in _events(sink)]
    assert [e["command"] for e in data] == ["[redacted-mcp-tool]", "mcp__other__read", "stat"]
    assert all(e["hook"] == "worker-permission" for e in data)
    stored = json.dumps([dispatch.load(droot, d["id"]), _events(sink)])
    for secret in ("helper.internal", "zq7suffix", "zq9arg"):
        assert secret not in stored


def test_a_hook_denials_summary_never_carries_an_unsafe_tool_name():
    from agent_cli.hooks import decision_log

    assert decision_log.summary({"tool_name": f"mcp__{URL}__read"}) == "[redacted-mcp-tool]"
    assert decision_log.summary({"tool_name": "mcp__extra__read"}) == "mcp__extra__read"
    assert decision_log.summary({"tool_input": {"command": f"{URL} -v"}}) == "[redacted-endpoint] -v"
    assert decision_log.summary({"tool_input": {"command": "/usr/bin/curl -v"}}) == "curl -v"


# ------------------------------------------------------------------ free text in events and telemetry


def _reasons(root: Path, d: dict) -> list[str]:
    return [e["reason"] for e in dispatch.load(root, d["id"])["permission_events"]]


def _main(root: Path, d: dict, monkeypatch, capsys, command: str) -> dict[str, Any]:
    """The Codex hook's whole stdout for one Bash call, as the worker's client reads it."""
    monkeypatch.setenv("AGENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_DISPATCH_ID", d["id"])
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    )
    capsys.readouterr()
    assert codex_permissions.main() == 0
    return json.loads(capsys.readouterr().out)


def test_codex_hook_reasons_reach_worker_and_record_without_an_endpoint(
    droot: Path, monkeypatch, capsys, sink: Path
):
    d = _dispatch(droot, monkeypatch, ["endpoint"])
    worktree = Path(d["worktree"])
    returned = []
    for target in (f"/tmp/{URL}", f"/tmp/{OTHER_URL}", "/tmp/plain-copy"):
        out = _main(droot, d, monkeypatch, capsys, f"cp notes.txt {target}")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        returned.append(out["hookSpecificOutput"]["permissionDecisionReason"])
    (worktree / "noisy.py").write_text(f"# feature 044 calls {URL} first\nx = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "noisy.py"], check=True, capture_output=True)
    out = _hook(droot, d, monkeypatch, "Bash", command="git commit -m checkpoint")
    returned.append(out["hookSpecificOutput"]["permissionDecisionReason"])
    escape = "Worker cp escapes the dispatch worktree: "
    stored = [
        f"{escape}/tmp/[redacted-endpoint]",
        f"{escape}/tmp/[redacted-endpoint]",
        f"{escape}/tmp/plain-copy",
        'noisy.py:1 cites "feature 044": "feature 044 calls [redacted-endpoint] first"',
    ]
    assert returned[:3] == [f"{s} {PROTECTED_NOTE}" for s in stored[:3]]
    assert stored[3] in returned[3]
    assert returned[3].endswith(f"\n{PROTECTED_NOTE}")
    stored = list(returned)
    assert _reasons(droot, d) == stored
    assert [(e["data"]["command"], e["data"]["reason"]) for e in _events(sink)] == [
        *[("cp", reason) for reason in stored[:3]],
        ("git -m", stored[3][:300]),
    ]
    everything = json.dumps([returned, dispatch.load(droot, d["id"]), _events(sink)])
    for secret in ("helper.internal", "zq7suffix", "elsewhere.internal", "zq8suffix"):
        assert secret not in everything


def test_claude_worker_reasons_reach_worker_and_record_without_an_endpoint(
    droot: Path, monkeypatch, sink: Path
):
    from claude_agent_sdk import ToolPermissionContext

    d = _dispatch(droot, monkeypatch, ["endpoint"])
    rec = dispatch.load(droot, d["id"])
    hooks = dispatch.rules.sdk_hooks(droot, Path(d["worktree"]), rec.get("start_sha"), d["id"])
    redirect = hooks["PreToolUse"][0].hooks[0]
    shown = []
    for target in (f"/tmp/{URL}", "/tmp/plain-copy"):
        payload: Any = {"tool_name": "Bash", "tool_input": {"command": f"cp notes.txt {target}"}}
        out: Any = asyncio.run(redirect(payload, None, {"signal": None}))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        shown.append(out["hookSpecificOutput"]["permissionDecisionReason"])
    escape = "Worker cp escapes the dispatch worktree: /tmp/"
    assert shown == [
        f"{escape}[redacted-endpoint] {PROTECTED_NOTE}",
        f"{escape}plain-copy {PROTECTED_NOTE}",
    ]
    callback = dispatch.supervisor.permission_callback(droot, rec, _alpha(droot))
    context = ToolPermissionContext(tool_use_id="toolu_0", agent_id=None)
    denied: Any = asyncio.run(callback("Bash", {"command": f"curl {OTHER_URL} && ls"}, context))
    assert denied.message.endswith(
        " Run `curl [redacted-endpoint] first, then the next command in a new Bash call."
    )
    assert [e["data"]["reason"] for e in _events(sink)][:2] == shown
    everything = json.dumps([shown, denied.message, dispatch.load(droot, d["id"]), _events(sink)])
    for secret in ("helper.internal", "zq7suffix", "elsewhere.internal", "zq8suffix"):
        assert secret not in everything


def test_profile_grants_hold_one_contract_on_both_providers(droot: Path, monkeypatch):
    d = _dispatch(droot, monkeypatch, ["exact"])
    verdicts = {"mcp__bare__read": "allow", "mcp__bare__write": "deny", "mcp__alpha__read": "allow"}
    assert {tool: _codex(droot, d, tool)[0] for tool in verdicts} == verdicts
    assert _codex(droot, d, "Bash", command="stat x y")[0] == "deny"  # the profile's deny wins
    assert _codex(droot, d, "Bash", command="stat x")[0] == "allow"
    bare = "dispatch.mcp profile 'bare' allow 'mcp__bare' is a bare server rule"
    for refused in (["bare"], ["helper", "bare"]):
        with pytest.raises(AgentError, match=bare):
            dispatch.build_settings(droot, _alpha(droot), mcp=refused)
        rec = _recorded(droot, d, refused)  # recorded before the profile changed: every tool refuses
        for tool in ("mcp__bare__read", "mcp__helper__read", "Bash"):
            with pytest.raises(AgentError, match=bare):
                _codex(droot, d, tool, command="stat README.md")
        with pytest.raises(AgentError, match=bare):
            dispatch.rules.decide("Bash", {"command": "stat README.md"}, rec, _alpha(droot), droot)


def test_a_host_decline_stores_its_detail_without_an_endpoint(droot: Path, monkeypatch, sink: Path):
    from agent_cli.dispatch import codex_transport

    d = _dispatch(droot, monkeypatch, ["endpoint"])
    rec = dispatch.load(droot, d["id"])
    method = "item/commandExecution/requestApproval"
    for n, params in enumerate(
        [
            {"command": f'/bin/zsh -lc "curl {URL} -o out.json"'},
            {"reason": f"write the config for {OTHER_URL} to disk"},
            {"command": "git push origin main"},
        ]
    ):
        codex_transport._host_decline(droot, rec, method, {"id": n}, params)  # pyright: ignore[reportPrivateUsage]
    host = [e for e in dispatch.load(droot, d["id"])["permission_events"] if e.get("origin") == "host"]
    assert [(e["request_id"], e["decision"], e["detail"]) for e in host] == [
        (0, "decline", '/bin/zsh -lc "curl [redacted-endpoint] -o out.json"'),
        (1, "decline", "write the config for [redacted-endpoint] to disk"),
        (2, "decline", "git push origin main"),
    ]
    assert all("no owner saw it" in e["reason"] for e in host)
    assert [(e["data"]["hook"], e["data"]["class"]) for e in _events(sink)] == [
        ("host-decline", "host-decline")
    ] * 3
    everything = json.dumps([dispatch.load(droot, d["id"]), _events(sink)])
    for secret in ("helper.internal", "zq7suffix", "elsewhere.internal", "zq8suffix"):
        assert secret not in everything
