"""Every effective MCP rule meets the grant contract, and a profile's known conflict with the
worker base policy refuses the launch.

A canonical grant (`mcp__<server>__*` or an exact tool) beside a bare or pattern rule naming the
same server is refused: Claude reads the bare sibling as the whole server and the Codex hook as
no tool, so the valid grant would hide two providers granting different tools. Alone, with no
canonical grant, such a rule refuses too; no rule at all and a fully denied grant do not. A profile rule the
base policy cancels outright refuses the launch the way `build_settings` does, through the same
`permissions.shadowed`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_cli import dispatch
from agent_cli import permissions as perms
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_permissions, settings
from agent_cli.dispatch import rules as claude_rules
from dispatch_helpers import _set_alpha_dispatch  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.usefixtures("notes_remote")

CONTRACT = Path(__file__).resolve().parents[1] / "harness" / "worker-contract.md"
BARE = "is a bare server rule, which Claude reads as the whole server and the Codex hook matches to no tool"
FIX = "write `mcp__helper__*` for the whole server or mcp__<server>__<tool> per tool"
OTHER = "is not an exact tool or wildcard rule for the selected server 'helper'"
NOT_A_RULE = "is not a permission rule (expected Tool, Tool(arg) or Tool(prefix:*))"

# allowed_tools, the canonical grant the refusal names, and each invalid sibling with its cause
MIXED = {
    "exact then bare": (["mcp__helper__read", "mcp__helper"], "read", {"'mcp__helper'": BARE}),
    "bare then exact": (["mcp__helper", "mcp__helper__read"], "read", {"'mcp__helper'": BARE}),
    "wildcard then bare": (["mcp__helper__*", "mcp__helper"], "*", {"'mcp__helper'": BARE}),
    "bare then wildcard": (["mcp__helper", "mcp__helper__*"], "*", {"'mcp__helper'": BARE}),
    "exact then glob": (["mcp__helper__read", "mcp__helper__re*"], "read", {"'mcp__helper__re*'": OTHER}),
    "glob then wildcard": (["mcp__helper*", "mcp__helper__*"], "*", {"'mcp__helper*'": OTHER}),
    "every server": (["mcp__*", "mcp__helper__read"], "read", {"'mcp__*'": OTHER}),
    "exact then malformed": (
        ["mcp__helper__read", "mcp__helper__read(https://helper.internal/zq7)"],
        "read",
        {"[redacted-mcp-rule]": OTHER},
    ),
    "malformed then wildcard": (["mcp__helper__x)", "mcp__helper__*"], "*", {"[redacted-mcp-rule]": OTHER}),
    "bare and glob": (
        ["mcp__helper", "mcp__helper__read", "mcp__helper__wr*"],
        "read",
        {"'mcp__helper'": BARE, "'mcp__helper__wr*'": OTHER},
    ),
}
DENIES = {
    "none": [],
    "one tool": ["mcp__helper__write"],
    "whole": ["mcp__helper__*"],
    "bare": ["mcp__helper"],
}


def _select_helper(root: Path, allowed_tools: list[str]) -> repos_mod.Repo:
    _set_alpha_dispatch(root, repo_mcp=["helper"], allowed_tools=allowed_tools)
    clone = root / "repos/alpha-api"
    (clone / ".mcp.json").write_text(json.dumps({"mcpServers": {"helper": {"command": "helper-server"}}}))
    return repos_mod.load_registry(root).require("alpha")


def _hook(root: Path, monkeypatch, tool: str, mcp: list[str] | None = None) -> tuple[str, str]:
    d = {"id": "999-99", "repo": "alpha", "worktree": str(root), "mcp": mcp or []}
    monkeypatch.setattr(codex_permissions.state, "load", lambda _root, _id: d)
    return codex_permissions._decision(root, "999-99", {"tool_name": tool, "tool_input": {}})


def _feature(root: Path, run) -> None:
    run(["feature", "new", "alpha-work", "--title", "Alpha work", "--repo", "alpha"])
    (root / "harness").mkdir(exist_ok=True)
    (root / "harness" / "worker-contract.md").write_text(
        CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )


def _dry_run(run, *mcp: str):
    args = ["dispatch", "new", "alpha", "--feature", "1", "--prompt", "qa", "--dry-run"]
    return run([*args, *[a for name in mcp for a in ("--mcp", name)]])


# ------------------------------------------------------------------ mixed repo grants


@pytest.mark.parametrize(
    ("case", "deny"),
    [
        *((case, "none") for case in MIXED),
        ("exact then bare", "one tool"),
        ("exact then bare", "whole"),
        ("exact then bare", "bare"),
    ],
)
def test_a_valid_grant_does_not_excuse_an_invalid_sibling(tmp_root: Path, monkeypatch, case, deny):
    allow, valid, invalid = MIXED[case]
    monkeypatch.setattr(settings, "BASE_DENY", [*settings.BASE_DENY, *DENIES[deny]])
    repo = _select_helper(tmp_root, allow)
    kind, rules = perms.mcp_server_grant("helper", allow, DENIES[deny])
    assert kind == "mixed" and [perms.shown_rule(r) for r in rules] == list(invalid), rules

    beside = f"beside the grant 'mcp__helper__{valid}'; {FIX}"
    refusals = [f"{shown} {cause}, {beside}" for shown, cause in invalid.items()]
    with pytest.raises(AgentError) as built:
        settings.build_settings(tmp_root, repo, worktree=str(tmp_root))
    lines = built.value.message.splitlines()[1:]
    assert [line for line in lines if "allowed_tools" in line] == [
        f"  agent.yaml alpha allowed_tools {refusal}" for refusal in refusals
    ], lines
    for tool in ("mcp__helper__read", "mcp__helper__write", "Bash"):
        with pytest.raises(AgentError, match="agent.yaml alpha allowed_tools"):
            _hook(tmp_root, monkeypatch, tool)
    for leak in ("helper.internal", "zq7", "https"):
        assert leak not in built.value.message, leak


@pytest.mark.parametrize(
    "allow", [["mcp__helper__read", "mcp__helper"], ["mcp__helper", "mcp__helper__read"]]
)
def test_public_dry_run_names_the_mixed_rule(tmp_root: Path, run, fake_claude, allow):
    _feature(tmp_root, run)
    _select_helper(tmp_root, allow)
    refusal = f"'mcp__helper' {BARE}, beside the grant 'mcp__helper__read'; {FIX}"
    launched = _dry_run(run)
    assert launched.code != 0
    assert f"agent.yaml alpha allowed_tools {refusal}" in launched.out + launched.err
    assert not (tmp_root / ".agent" / "dispatches").exists()


CANONICAL = {
    "exact": (["mcp__helper__read"], [], {"read": "allow", "write": "deny"}),
    "wildcard": (["mcp__helper__*"], [], {"read": "allow", "write": "allow"}),
    "exact and wildcard": (["mcp__helper__read", "mcp__helper__*"], [], {"read": "allow", "write": "allow"}),
    "wildcard and exact": (["mcp__helper__*", "mcp__helper__read"], [], {"read": "allow", "write": "allow"}),
    "deny one tool": (["mcp__helper__*", "mcp__helper__read"], ["mcp__helper__write"], {"write": "deny"}),
    "another server": (["mcp__helper__read", "mcp__helpers"], [], {"read": "allow", "write": "deny"}),
}


@pytest.mark.parametrize("case", CANONICAL)
def test_canonical_siblings_still_grant_and_match_raw(tmp_root: Path, monkeypatch, case):
    allow, deny, verdicts = CANONICAL[case]
    monkeypatch.setattr(settings, "BASE_DENY", [*settings.BASE_DENY, *deny])
    repo = _select_helper(tmp_root, allow)
    built = settings.build_settings(tmp_root, repo, worktree=str(tmp_root))["permissions"]
    assert [r for r in built["allow"] if r.startswith("mcp__helper")] == allow
    assert perms.mcp_server_grant("helper", allow, built["deny"])[0] == "granted"
    for tool, verdict in verdicts.items():
        assert _hook(tmp_root, monkeypatch, f"mcp__helper__{tool}")[0] == verdict, tool


def test_the_base_lists_canonical_grant_counts_as_a_valid_sibling(tmp_root: Path):
    """`mcp__agent__log` is in every worker's allow list, so a repo selecting a server named
    `agent` already has a canonical grant beside any bare rule it adds."""
    _set_alpha_dispatch(tmp_root, repo_mcp=["agent"], allowed_tools=["mcp__agent"])
    refusal = (
        f"'mcp__agent' {BARE}, beside the grant 'mcp__agent__log'; write `mcp__agent__*` for the whole "
        "server or mcp__<server>__<tool> per tool"
    )
    with pytest.raises(AgentError) as exc:
        settings.build_settings(tmp_root, repos_mod.load_registry(tmp_root).require("alpha"))
    assert f"  agent.yaml alpha allowed_tools {refusal}" in exc.value.message.splitlines()


# ------------------------------------------------------------------ no canonical grant at all

# allowed_tools, and each refused rule with its cause
ONLY = {
    "bare": (["mcp__helper"], {"'mcp__helper'": BARE}),
    "bare beside Bash": (["Bash(stat x:*)", "mcp__helper"], {"'mcp__helper'": BARE}),
    "bare and glob": (
        ["mcp__helper", "mcp__helper__re*"],
        {"'mcp__helper'": BARE, "'mcp__helper__re*'": OTHER},
    ),
    "prefix glob": (["mcp__helper*"], {"'mcp__helper*'": OTHER}),
    "partial glob": (["mcp__helper__re*"], {"'mcp__helper__re*'": OTHER}),
    "every server": (["mcp__*"], {"'mcp__*'": OTHER}),
    "argument": (["mcp__helper__read(x)"], {"[redacted-mcp-rule]": OTHER}),
}


@pytest.mark.parametrize(
    ("case", "deny"),
    [*((case, "none") for case in ONLY), ("bare", "one tool"), ("bare", "whole"), ("bare", "bare")],
)
def test_a_rule_without_a_canonical_grant_refuses_the_launch(tmp_root: Path, monkeypatch, case, deny):
    allow, invalid = ONLY[case]
    monkeypatch.setattr(settings, "BASE_DENY", [*settings.BASE_DENY, *DENIES[deny]])
    repo = _select_helper(tmp_root, allow)
    assert perms.mcp_server_grant("helper", allow, DENIES[deny])[0] in ("bare", "other")

    refusals = [f"agent.yaml alpha allowed_tools {shown} {cause}; {FIX}" for shown, cause in invalid.items()]
    assert settings.selection_problems(repo, []) == refusals
    with pytest.raises(AgentError) as built:
        settings.build_settings(tmp_root, repo, worktree=str(tmp_root))
    lines = built.value.message.splitlines()[1:]
    assert [line for line in lines if "allowed_tools" in line] == [f"  {r}" for r in refusals], lines
    for tool in ("mcp__helper__read", "mcp__helper__write", "Bash"):
        with pytest.raises(AgentError, match="agent.yaml alpha allowed_tools"):
            _hook(tmp_root, monkeypatch, tool)
    d = {"id": "999-99", "repo": "alpha", "worktree": str(tmp_root), "mcp": []}
    with pytest.raises(AgentError, match="agent.yaml alpha allowed_tools"):
        claude_rules.decide("Bash", {"command": "ls"}, d, repo, tmp_root)


# allowed_tools, extra base deny, and the Codex verdict per tool
UNREFUSED = {
    "missing": (["Bash(stat x:*)"], [], "deny"),
    "another server": (["mcp__helpers__*"], [], "deny"),
    "fully denied": (["mcp__helper__*"], ["mcp__*"], "deny"),
    "exact": (["mcp__helper__read"], [], "allow"),
}


@pytest.mark.parametrize("case", UNREFUSED)
def test_absence_full_denial_and_a_valid_grant_still_launch(tmp_root: Path, monkeypatch, case):
    allow, deny, verdict = UNREFUSED[case]
    monkeypatch.setattr(settings, "BASE_DENY", [*settings.BASE_DENY, *deny])
    repo = _select_helper(tmp_root, allow)
    assert settings.selection_problems(repo, []) == []
    built = settings.build_settings(tmp_root, repo, worktree=str(tmp_root))["permissions"]
    assert all(rule in built["allow"] for rule in allow)
    assert _hook(tmp_root, monkeypatch, "mcp__helper__read")[0] == verdict


def test_a_rule_lint_refuses_keeps_only_its_own_line(tmp_root: Path):
    repo = _select_helper(tmp_root, ["mcp__helper__x)"])
    assert settings.selection_problems(repo, []) == []
    with pytest.raises(AgentError) as built:
        settings.build_settings(tmp_root, repo)
    assert built.value.message.splitlines()[1:] == [f"  alpha worker allow: [redacted-mcp-rule] {NOT_A_RULE}"]


def test_a_pattern_naming_a_withheld_server_is_withheld(tmp_root: Path):
    secret = "https://secret.example/token"
    _set_alpha_dispatch(tmp_root, repo_mcp=[secret], allowed_tools=["mcp__https*"])
    with pytest.raises(AgentError) as built:
        settings.build_settings(tmp_root, repos_mod.load_registry(tmp_root).require("alpha"))
    assert built.value.message.splitlines()[1:] == [
        "  agent.yaml alpha allowed_tools [redacted-mcp-rule] is not an exact tool or wildcard rule for the "
        "selected server '[redacted-name]'; write `mcp__<server>__*` for the whole server or "
        "mcp__<server>__<tool> per tool"
    ]


def test_a_repo_bare_only_rule_refuses_as_a_profile_one_does(tmp_root: Path):
    _profiles(tmp_root, SOURCES)
    _set_alpha_dispatch(tmp_root, repo_mcp=[], allowed_tools=[])
    assert _refusal(tmp_root, ["bare"]) == [
        f"  dispatch.mcp profile 'bare' allow 'mcp__helper' {BARE}; {FIX}"
    ]
    _select_helper(tmp_root, ["mcp__helper"])
    assert _refusal(tmp_root, []) == [f"  agent.yaml alpha allowed_tools 'mcp__helper' {BARE}; {FIX}"]


def _refusal(root: Path, mcp: list[str]) -> list[str]:
    with pytest.raises(AgentError) as exc:
        settings.build_settings(root, repos_mod.load_registry(root).require("alpha"), mcp=mcp)
    return exc.value.message.splitlines()[1:]


def test_public_dry_run_and_launch_refuse_a_bare_only_grant(tmp_root: Path, run, fake_claude):
    _feature(tmp_root, run)
    _select_helper(tmp_root, ["mcp__helper"])
    refusal = f"agent.yaml alpha allowed_tools 'mcp__helper' {BARE}; {FIX}"
    launch = ["dispatch", "new", "alpha", "--feature", "1", "--prompt", "qa", "--no-checks"]
    for launched in (_dry_run(run), run(launch)):
        assert launched.code != 0 and refusal in launched.out + launched.err, launched.err
    assert not (tmp_root / ".agent" / "dispatches").exists()
    clone = tmp_root / "repos/alpha-api"
    listed = subprocess.run(["git", "-C", str(clone), "worktree", "list"], capture_output=True, text=True)
    assert len(listed.stdout.splitlines()) == 1, listed.stdout


def test_an_ambiguous_selection_still_refuses_before_any_grant(tmp_root: Path):
    _set_alpha_dispatch(tmp_root, repo_mcp=["a__b"], allowed_tools=["mcp__a__b__read", "mcp__a__b"])
    assert perms.mcp_server_grant("a__b", ["mcp__a__b__read", "mcp__a__b"]) == ("ambiguous", [])
    with pytest.raises(AgentError, match="repo_mcp selects 'a__b'") as exc:
        settings.build_settings(tmp_root, repos_mod.load_registry(tmp_root).require("alpha"))
    assert "allowed_tools" not in exc.value.message


# ------------------------------------------------------------------ repo and profile sources

SOURCES = """
worker_mcp:
  exact: {server: helper, command: s, allow: [mcp__helper__read], deny: [mcp__helper__private]}
  bare: {server: helper, command: s, allow: [mcp__helper]}
"""


def _profiles(root: Path, block: str) -> None:
    reg = root / "agent.yaml"
    reg.write_text(reg.read_text(encoding="utf-8") + block, encoding="utf-8")


@pytest.mark.parametrize("allow", [["mcp__helper", "mcp__alpha__*"], ["mcp__alpha__*", "mcp__helper"]])
def test_a_repo_bare_rule_beside_a_profile_grant_names_the_repo(tmp_root: Path, monkeypatch, allow):
    _profiles(tmp_root, SOURCES)
    _set_alpha_dispatch(tmp_root, repo_mcp=[], allowed_tools=allow)
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    assert "mcp__helper" in settings.build_settings(tmp_root, repo)["permissions"]["allow"]  # no profile
    with pytest.raises(AgentError) as exc:
        settings.build_settings(tmp_root, repo, mcp=["exact"])
    assert exc.value.message.splitlines()[1:] == [
        f"  agent.yaml alpha allowed_tools 'mcp__helper' {BARE}, beside the grant 'mcp__helper__read'; {FIX}"
    ]
    with pytest.raises(AgentError, match="agent.yaml alpha allowed_tools 'mcp__helper'"):
        _hook(tmp_root, monkeypatch, "mcp__helper__read", ["exact"])


@pytest.mark.parametrize("allow", [["mcp__helper__read", "mcp__helper__*"], ["mcp__helper__*"]])
def test_a_profile_bare_rule_beside_a_repo_grant_names_the_profile(tmp_root: Path, allow):
    _profiles(tmp_root, SOURCES)
    _set_alpha_dispatch(tmp_root, repo_mcp=[], allowed_tools=allow)
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    with pytest.raises(AgentError) as exc:
        settings.build_settings(tmp_root, repo, mcp=["bare"])
    assert exc.value.message.splitlines()[1:] == [
        f"  dispatch.mcp profile 'bare' allow 'mcp__helper' {BARE}; {FIX}"
    ]


def test_profile_deny_priority_holds_beside_a_repo_wildcard(tmp_root: Path, monkeypatch):
    _profiles(tmp_root, SOURCES)
    _set_alpha_dispatch(tmp_root, repo_mcp=[], allowed_tools=["mcp__helper__*"])
    verdicts = {"mcp__helper__read": "allow", "mcp__helper__write": "allow", "mcp__helper__private": "deny"}
    assert {t: _hook(tmp_root, monkeypatch, t, ["exact"])[0] for t in verdicts} == verdicts


# ------------------------------------------------------------------ profile against the base policy

CONFLICTS = """
worker_mcp:
  push: {server: p1, command: s, allow: ["Bash(git push:*)", "Bash(stat x:*)"]}
  webdeny: {server: p2, command: s, deny: [WebFetch]}
  near: {server: p3, command: s, allow: ["Bash(git push origin:*)"]}
  safe: {server: p4, command: s, allow: ["mcp__p4__*", "Bash(stat x:*)"], deny: ["Bash(stat x y:*)"]}
  pr: {server: p5, command: s, allow: ["Bash(gh pr create:*)"]}
"""


def _beta_on_gitlab(root: Path) -> None:
    reg = root / "agent.yaml"
    data = yaml.safe_load(reg.read_text(encoding="utf-8"))
    next(r for r in data["repos"] if r["short"] == "beta")["forge"] = "gitlab"
    reg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_each_known_base_conflict_is_a_build_settings_refusal(tmp_root: Path):
    _profiles(tmp_root, CONFLICTS)
    _beta_on_gitlab(tmp_root)
    registry = repos_mod.load_registry(tmp_root)
    alpha, beta = registry.require("alpha"), registry.require("beta")
    for repo, profile, rule in (
        (alpha, "push", "allow 'Bash(git push:*)'"),
        (beta, "push", "allow 'Bash(git push:*)'"),
        (alpha, "webdeny", "allow 'WebFetch'"),
        (alpha, "pr", "allow 'Bash(gh pr create:*)'"),
    ):
        with pytest.raises(AgentError) as exc:
            settings.build_settings(tmp_root, repo, mcp=[profile])
        assert f"  {rule} is cancelled by the same rule in deny" in exc.value.message.splitlines(), profile
    assert (
        "Bash(gh pr create:*)" in settings.build_settings(tmp_root, beta, mcp=["pr"])["permissions"]["allow"]
    )


def test_runtime_denials_do_not_refuse_the_launch_and_grant_nothing(tmp_root: Path):
    _profiles(tmp_root, CONFLICTS)
    alpha = repos_mod.load_registry(tmp_root).require("alpha")
    near = settings.build_settings(tmp_root, alpha, mcp=["near"])["permissions"]
    assert perms.matches("git push origin main", near["allow"], near["deny"]) == (
        "denied",
        "Bash(git push:*)",
    )
    safe = settings.build_settings(tmp_root, alpha, mcp=["safe"])["permissions"]
    assert perms.matches("stat x", safe["allow"], safe["deny"])[0] == "allowed"
    assert perms.matches("stat x y", safe["allow"], safe["deny"])[0] == "denied"


def test_public_dry_run_carries_the_conflict(tmp_root: Path, run, fake_claude):
    _feature(tmp_root, run)
    _profiles(tmp_root, CONFLICTS)
    refused = _dry_run(run, "push")
    assert (
        refused.code != 0 and "allow 'Bash(git push:*)' is cancelled by the same rule in deny" in refused.err
    )
    assert _dry_run(run, "near").code == 0
    assert not (tmp_root / ".agent" / "dispatches").exists()


def test_no_profile_builds_settings(tmp_root: Path):
    assert dispatch.build_settings(tmp_root, repos_mod.load_registry(tmp_root).require("alpha"))
