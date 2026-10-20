"""The MCP grant contract case by case: what Claude's settings and the Codex hook each make of one
repo's grant, and a rule padded with whitespace.

A padded rule naming a selected server refuses with its own cause, alone or beside a canonical
grant, from a repo or a profile: `lint` strips a rule before it parses it, so the padding must not
carry it past the grant contract. The rule is never trimmed; diagnostics quote it raw. A padded
Bash rule and a padded rule for an unselected server keep their behavior.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import dispatch, worker_mcp
from agent_cli import permissions as perms
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_permissions, settings
from agent_cli.dispatch import rules as claude_rules
from dispatch_helpers import (
    _set_alpha_dispatch,  # pyright: ignore[reportPrivateUsage]
    droot,  # noqa: F401 -- pytest fixture, used only as a parameter name
)

REFUSE = "refuse"
FAILED = "Codex dispatch policy failed: AgentError"
BARE = "is a bare server rule, which Claude reads as the whole server and the Codex hook matches to no tool"


def _other(server: str, owner: str = "the selected server") -> str:
    return f"is not an exact tool or wildcard rule for {owner} {server!r}"


def _fix(server: str) -> str:
    return f"write `mcp__{server}__*` for the whole server or mcp__<server>__<tool> per tool"


def _outer_hook(root: Path, monkeypatch, capsys, dispatch_id: str, tool: str, **tool_input: str) -> dict:
    """The Codex hook's whole stdout for one call, as the worker's client reads it."""
    monkeypatch.setenv("AGENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_DISPATCH_ID", dispatch_id)
    event = {"tool_name": tool, "tool_input": tool_input}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    capsys.readouterr()
    assert codex_permissions.main() == 0
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]


# ------------------------------------------------------------------ one MCP grant contract
#
# Each row: the repo's allow rules, an extra worker deny rule, what the shared grant classifier
# says, and what the Codex worker hook decides for each tool. `acme` against `acmeplus` is
# the server-prefix separation. Claude's own matching is native and is not exercised here: the
# Claude rows prove the rules reach its settings verbatim and the fall-through callback never
# allows an MCP call. A bare or pattern rule with no canonical grant refuses the launch, and every
# tool call, on both providers.
MCP_CASES = {
    "exact": (["mcp__acme__read"], [], "granted", {"read": "allow", "write": "deny", "sketch": "deny"}),
    "wildcard": (["mcp__acme__*"], [], "granted", {"read": "allow", "write": "allow", "sketch": "deny"}),
    "bare": (["mcp__acme"], [], "bare", {"read": REFUSE, "write": REFUSE, "sketch": REFUSE}),
    "wrong server": (["mcp__other__*"], [], "none", {"read": "deny", "sketch": "deny"}),
    "prefix glob": (["mcp__acme*"], [], "other", {"read": REFUSE, "sketch": REFUSE}),
    "partial glob": (["mcp__acme__re*"], [], "other", {"read": REFUSE, "write": REFUSE}),
    "deny one tool": (
        ["mcp__acme__*"],
        ["mcp__acme__write"],
        "granted",
        {"read": "allow", "write": "deny"},
    ),
    "deny all": (["mcp__acme__*"], ["mcp__*"], "denied", {"read": "deny", "write": "deny"}),
}
MCP_TOOLS = {"read": "mcp__acme__read", "write": "mcp__acme__write", "sketch": "mcp__acmeplus__read"}
# the refusal a case with no canonical grant makes
MCP_REFUSALS = {
    "bare": f"'mcp__acme' {BARE}; {_fix('acme')}",
    "prefix glob": f"'mcp__acme*' {_other('acme')}; {_fix('acme')}",
    "partial glob": f"'mcp__acme__re*' {_other('acme')}; {_fix('acme')}",
}


@pytest.mark.parametrize("case", MCP_CASES)
def test_one_mcp_grant_contract_for_claude_and_codex(tmp_root, monkeypatch, capsys, case):
    allow, deny, grant, codex = MCP_CASES[case]
    monkeypatch.setattr(settings, "BASE_DENY", [*settings.BASE_DENY, *deny])
    _set_alpha_dispatch(tmp_root, allowed_tools=allow, repo_mcp=["acme"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    worktree = tmp_root / "wt"
    worktree.mkdir()
    d = {"id": "999-99", "repo": "alpha", "worktree": str(worktree)}
    monkeypatch.setattr(codex_permissions.state, "load", lambda root, dispatch_id: d)
    assert (
        perms.mcp_server_grant("acme", list(repo.allowed_tools), settings.base_policy(tmp_root, repo)[1])[0]
        == grant
    )

    refusal = ""
    if case in MCP_REFUSALS:
        refusal = f"agent.yaml alpha allowed_tools {MCP_REFUSALS[case]}"
        assert settings.selection_problems(repo, []) == [refusal]
        with pytest.raises(AgentError) as built:
            settings.build_settings(tmp_root, repo, worktree=str(worktree))
        assert f"  {refusal}" in built.value.message.splitlines(), built.value.message
    else:
        perms_ = settings.build_settings(tmp_root, repo, worktree=str(worktree))["permissions"]
        granted = [r for r in perms_["allow"] if r.startswith("mcp__") and r != "mcp__agent__log"]
        assert granted == allow, "Claude gets the grants verbatim"
        assert all(r in perms_["deny"] for r in deny), "no deny is dropped"

    for key, expected in codex.items():
        event = {"tool_name": MCP_TOOLS[key], "tool_input": {}}
        assert claude_rules.decide(MCP_TOOLS[key], {}, d, repo, tmp_root)[0] == "deny"
        if expected != REFUSE:
            assert codex_permissions._decision(tmp_root, "999-99", event)[0] == expected, (case, key)
            continue
        with pytest.raises(AgentError, match=re.escape(refusal)):
            codex_permissions._decision(tmp_root, "999-99", event)
        out = _outer_hook(tmp_root, monkeypatch, capsys, "999-99", MCP_TOOLS[key])
        assert (out["permissionDecision"], out["permissionDecisionReason"]) == ("deny", FAILED)
    if refusal:
        with pytest.raises(AgentError, match=re.escape(refusal)):
            claude_rules.decide("Bash", {"command": "ls"}, d, repo, tmp_root)


# ------------------------------------------------------------------ whitespace around a rule

FIX = _fix("helper")
SPACED = f"has whitespace around it, so it {_other('helper')}"
PROFILE_OWNER = "the profile's server"
PROFILE_SPACED = f"has whitespace around it, so it {_other('helper', PROFILE_OWNER)}"
PADDED = [
    " mcp__helper__read",
    "mcp__helper__read ",
    "\tmcp__helper__*",
    "mcp__helper__*\n",
    " mcp__helper",
    "mcp__helper\t",
]
PROFILES = """
worker_mcp:
  padded: {server: helper, command: s, allow: [" mcp__helper__read"]}
  beside: {server: helper, command: s, allow: [mcp__helper__read, "mcp__helper__*\\t"]}
  padded-deny: {server: helper, command: s, allow: [mcp__helper__*], deny: ["mcp__helper__write\\n"]}
  exact: {server: helper, command: s, allow: [mcp__helper__read]}
"""
PROFILE_REFUSALS = {
    "padded": f"dispatch.mcp profile 'padded' allow ' mcp__helper__read' {PROFILE_SPACED}; {FIX}",
    "beside": f"dispatch.mcp profile 'beside' allow 'mcp__helper__*\\t' {PROFILE_SPACED}; {FIX}",
    "padded-deny": f"dispatch.mcp profile 'padded-deny' deny 'mcp__helper__write\\n' {PROFILE_SPACED}; {FIX}",
}


def _select_helper(
    root: Path, allowed_tools: list[str], repo_mcp: tuple[str, ...] = ("helper",)
) -> repos_mod.Repo:
    reg = root / "agent.yaml"
    if "worker_mcp:" not in reg.read_text(encoding="utf-8"):
        reg.write_text(reg.read_text(encoding="utf-8") + PROFILES, encoding="utf-8")
    _set_alpha_dispatch(root, repo_mcp=list(repo_mcp), allowed_tools=allowed_tools)
    clone = root / "repos/alpha-api"
    (clone / ".mcp.json").write_text(json.dumps({"mcpServers": {"helper": {"command": "helper-server"}}}))
    return repos_mod.load_registry(root).require("alpha")


def _hook(root: Path, monkeypatch, tool: str, mcp: list[str] | None = None, **tool_input: str):
    d = {"id": "999-99", "repo": "alpha", "worktree": str(root), "mcp": mcp or []}
    monkeypatch.setattr(codex_permissions.state, "load", lambda _root, _id: d)
    return codex_permissions._decision(root, "999-99", {"tool_name": tool, "tool_input": tool_input}), d


def _refused_everywhere(root: Path, monkeypatch, repo, refusal: str, mcp: list[str]) -> None:
    """`build_settings`, the Codex hook and Claude's fallback for Bash all refuse with `refusal`;
    the fallback denies an MCP call without reading the rules."""
    with pytest.raises(AgentError) as built:
        settings.build_settings(root, repo, worktree=str(root), mcp=mcp)
    assert [line for line in built.value.message.splitlines()[1:] if "mcp__helper" in line] == [
        f"  {refusal}"
    ]
    d = {"id": "999-99", "repo": "alpha", "worktree": str(root), "mcp": mcp}
    for tool in ("mcp__helper__read", "mcp__helper__write", "Bash"):
        with pytest.raises(AgentError, match=re.escape(refusal)):
            _hook(root, monkeypatch, tool, mcp, command="stat x")
    with pytest.raises(AgentError, match=re.escape(refusal)):
        claude_rules.decide("Bash", {"command": "stat x"}, d, repo, root)
    assert claude_rules.decide("mcp__helper__read", {}, d, repo, root)[0] == "deny"


@pytest.mark.parametrize("beside", [None, "mcp__helper__read", "mcp__helper__*"])
@pytest.mark.parametrize("rule", PADDED)
def test_a_padded_repo_rule_naming_the_selected_server_refuses(tmp_root: Path, monkeypatch, rule, beside):
    allow = [rule] if beside is None else [beside, rule]
    repo = _select_helper(tmp_root, allow)
    kind, rules = perms.mcp_server_grant("helper", allow)
    assert (kind, rules) == ("other" if beside is None else "mixed", [rule])
    grant = "" if beside is None else f", beside the grant {beside!r}"
    refused = f"{rule!r} {SPACED}{grant}; {FIX}"
    refusal = f"agent.yaml alpha allowed_tools {refused}"
    assert settings.selection_problems(repo, []) == [refusal]
    _refused_everywhere(tmp_root, monkeypatch, repo, refusal, [])


@pytest.mark.parametrize("profile", PROFILE_REFUSALS)
def test_a_padded_profile_rule_refuses_its_launch(tmp_root: Path, monkeypatch, profile):
    repo = _select_helper(tmp_root, [], repo_mcp=())
    refusal = PROFILE_REFUSALS[profile]
    assert settings.selection_problems(repo, worker_mcp.require(tmp_root, [profile])) == [refusal]
    _refused_everywhere(tmp_root, monkeypatch, repo, refusal, [profile])


@pytest.mark.parametrize("rule", ["mcp__helper__read ", " mcp__helper", "\nmcp__helper__*"])
def test_a_padded_repo_rule_for_a_profile_selected_server_refuses(tmp_root: Path, monkeypatch, rule):
    repo = _select_helper(tmp_root, [rule], repo_mcp=())
    assert settings.build_settings(tmp_root, repo)["permissions"]["allow"].count(rule) == 1  # no profile
    refusal = f"agent.yaml alpha allowed_tools {rule!r} {SPACED}, beside the grant 'mcp__helper__read'; {FIX}"
    _refused_everywhere(tmp_root, monkeypatch, repo, refusal, ["exact"])


def test_padded_bash_and_unselected_server_rules_and_exact_grants_keep_working(tmp_root: Path, monkeypatch):
    padded = [" Bash(stat x:*)", "mcp__other__read ", " mcp__other__*", "mcp__helper__read", "mcp__helper__*"]
    repo = _select_helper(tmp_root, padded)
    assert settings.selection_problems(repo, []) == []
    built = settings.build_settings(tmp_root, repo, worktree=str(tmp_root))["permissions"]
    assert [r for r in built["allow"] if r in padded] == padded, "every rule reaches Claude untrimmed"
    assert perms.mcp_server_grant("helper", padded) == ("granted", ["mcp__helper__read", "mcp__helper__*"])
    verdicts = {
        "mcp__helper__read": ("allow", "Allowed by dispatch MCP rule"),
        "mcp__helper__write": ("allow", "Allowed by dispatch MCP rule"),
        "mcp__other__read": ("deny", "No dispatch MCP rule allows this tool"),
    }
    assert {tool: _hook(tmp_root, monkeypatch, tool)[0] for tool in verdicts} == verdicts
    assert _hook(tmp_root, monkeypatch, "Bash", command="stat x")[0] == ("allow", " Bash(stat x:*)")
    assert perms.matches("stat x", built["allow"], built["deny"]) == ("allowed", " Bash(stat x:*)")


# ------------------------------------------------------------------ public and persisted paths


def _feature(root: Path, run) -> None:
    run(["feature", "new", "alpha-work", "--title", "Alpha work", "--repo", "alpha"])
    contract = Path(__file__).resolve().parents[1] / "harness" / "worker-contract.md"
    (root / "harness").mkdir(exist_ok=True)
    (root / "harness" / "worker-contract.md").write_text(
        contract.read_text(encoding="utf-8"), encoding="utf-8"
    )


PUBLIC = {
    "repo alone": (
        ["mcp__helper__read "],
        ("helper",),
        [],
        f"agent.yaml alpha allowed_tools 'mcp__helper__read ' {SPACED}; {FIX}",
    ),
    "repo beside": (
        ["mcp__helper__read", "\tmcp__helper"],
        ("helper",),
        [],
        f"agent.yaml alpha allowed_tools '\\tmcp__helper' {SPACED}, beside the grant 'mcp__helper__read'; {FIX}",
    ),
    "profile": ([], (), ["padded"], PROFILE_REFUSALS["padded"]),
}


@pytest.mark.parametrize("case", PUBLIC)
def test_public_dry_run_and_launch_refuse_a_padded_rule(tmp_root: Path, notes_remote, run, fake_claude, case):
    allow, repo_mcp, mcp, refusal = PUBLIC[case]
    _feature(tmp_root, run)
    _select_helper(tmp_root, allow, repo_mcp)
    selected = [a for name in mcp for a in ("--mcp", name)]
    launch = ["dispatch", "new", "alpha", "--feature", "1", "--prompt", "qa", *selected]
    for launched in (run([*launch, "--dry-run"]), run([*launch, "--no-checks"])):
        assert launched.code != 0 and refusal in launched.out + launched.err, launched.err
    assert not (tmp_root / ".agent" / "dispatches").exists()
    clone = tmp_root / "repos/alpha-api"
    listed = subprocess.run(["git", "-C", str(clone), "worktree", "list"], capture_output=True, text=True)
    assert len(listed.stdout.splitlines()) == 1, listed.stdout


@pytest.mark.parametrize("case", ["repo", "profile"])
def test_a_recorded_dispatch_fails_closed_once_a_padded_rule_appears(droot: Path, monkeypatch, capsys, case):
    from dispatch_helpers import _new_fast  # pyright: ignore[reportPrivateUsage]

    _select_helper(droot, ["mcp__helper__read", " Bash(stat x:*)"])
    clone = droot / "repos/alpha-api"
    for git in (["add", ".mcp.json"], ["commit", "-qm", "product mcp"]):
        subprocess.run(["git", "-C", str(clone), *git], check=True, capture_output=True)
    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda self, *a, **k: None})
    )
    d = _new_fast(droot, watch=False, base="HEAD")
    assert _outer_hook(droot, monkeypatch, capsys, d["id"], "mcp__helper__read") == {
        "hookEventName": "PreToolUse"
    }
    assert _outer_hook(droot, monkeypatch, capsys, d["id"], "Bash", command="stat x") == {
        "hookEventName": "PreToolUse"
    }
    events = dispatch.load(droot, d["id"])["permission_events"]
    assert [(e["tool"], e["decision"]) for e in events] == [("mcp__helper__read", "allow"), ("Bash", "allow")]

    if case == "repo":
        _set_alpha_dispatch(
            droot, allowed_tools=["mcp__helper__read", " Bash(stat x:*)", "mcp__helper__read\t"]
        )
        refusal = f"'mcp__helper__read\\t' {SPACED}, beside the grant 'mcp__helper__read'"
    else:
        rec = dispatch.load(droot, d["id"])
        rec["mcp"] = ["padded"]
        dispatch.save(droot, rec)
        refusal = PROFILE_REFUSALS["padded"]
    rec = dispatch.load(droot, d["id"])
    repo = repos_mod.load_registry(droot).require("alpha")
    for tool in ("mcp__helper__read", "Bash"):
        out = _outer_hook(droot, monkeypatch, capsys, d["id"], tool, command="stat x")
        assert (out["permissionDecision"], out["permissionDecisionReason"]) == ("deny", FAILED)
        with pytest.raises(AgentError, match=re.escape(refusal)):
            codex_permissions.decision(
                droot, d["id"], {"tool_name": tool, "tool_input": {"command": "stat x"}}
            )
    with pytest.raises(AgentError, match=re.escape(refusal)):
        claude_rules.decide("Bash", {"command": "stat x"}, rec, repo, droot)
    assert dispatch.load(droot, d["id"])["permission_events"] == events, "a refused call grants nothing"
