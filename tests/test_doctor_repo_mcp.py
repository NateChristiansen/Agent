"""Repo MCP selections as a dispatch sees them, and the registry's doctor rows.

A repo whose `.mcp.json` servers `dispatch.allowed_tools` grants none of loads them unusable
into every dispatch. Doctor's grant and launch rows moved; the launch path refuses the same
selections, which these tests hold.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from agent_cli.core import AgentError
from agent_cli.doctor import checks_repos
from dispatch_helpers import _set_alpha_dispatch  # pyright: ignore[reportPrivateUsage]


def _register(root: Path, dispatch: str) -> None:
    reg = root / "agent.yaml"
    reg.write_text(
        reg.read_text()
        + "  - name: Gamma\n    short: gamma\n    path: repos/Gamma\n"
        + "    remote: git@github.com:example/Gamma.git\n    default_branch: main\n"
        + f"    dispatch: {dispatch}\n",
        encoding="utf-8",
    )


def test_worktrees_dir_warns_for_every_repo_without_one(tmp_root: Path):
    """Not only repos with dispatch history: beta has no clone and no dispatches."""
    _register(tmp_root, "{}")
    rows = {c["check"]: c["level"] for c in checks_repos.check_registry(tmp_root)}
    assert rows["repo beta worktrees_dir"] == "warn"
    assert rows["repo gamma worktrees_dir"] == "warn"
    assert "repo alpha worktrees_dir" not in rows


SHORT, LONG = "https://secret.example", "https://secret.example/token"
BROKEN = "permission rules are broken; fix them before dispatching:\n  "
NOT_A_RULE = "[redacted-mcp-rule] is not a permission rule (expected Tool, Tool(arg) or Tool(prefix:*))"
MID = (
    "[redacted-rule] stops mid-token, so it matches nothing. A prefix rule has to end on a whole token: "
    "write a prefix that ends on a whole token or spell the command out."
)
MIXED = (
    "[redacted-rule] mixes `*` with the trailing `:*`, so Claude Code reads the whole rule as a literal "
    "prefix and it matches nothing. Write the prefix with one trailing `*` and no `:`."
)


def _built(root: Path):
    from agent_cli import repos as repos_mod
    from agent_cli.dispatch import settings

    return settings.build_settings(root, repos_mod.load_registry(root).require("alpha"), worktree=str(root))


def _hook(root: Path, monkeypatch, tool: str):
    from agent_cli.dispatch import codex_permissions

    d = {"id": "999-99", "repo": "alpha", "worktree": str(root)}
    monkeypatch.setattr(codex_permissions.state, "load", lambda _root, _id: d)
    return codex_permissions._decision(root, "999-99", {"tool_name": tool, "tool_input": {}})


@pytest.mark.parametrize("selected", [[SHORT, LONG], [LONG, SHORT]])
def test_overlapping_selected_names_leave_no_suffix_on_any_surface(tmp_root: Path, monkeypatch, selected):
    rules = [f"Bash(curl {LONG}/:*)", f"Bash(curl {LONG} * x:*)"]
    _set_alpha_dispatch(tmp_root, repo_mcp=selected, allowed_tools=rules)
    with pytest.raises(AgentError) as built:
        _built(tmp_root)
    with pytest.raises(AgentError) as hooked:
        _hook(tmp_root, monkeypatch, "mcp__x__read")
    expected = f"{BROKEN}alpha worker allow: {MID}\n  alpha worker allow: {MIXED}"
    assert str(built.value) == str(hooked.value) == expected


AMBIGUOUS = {
    "delimiter": (["a", "a__b"], "mcp__a__b__read", "'a__b'"),
    "trailing underscore": (["a", "a_"], "mcp__a___read", "'a_'"),
    "unsafe name": (["a", f"a__{LONG}"], "mcp__a__read", "'[redacted-name]'"),
}


@pytest.mark.parametrize("case", AMBIGUOUS)
def test_a_server_name_holding_the_rule_delimiter_is_refused(tmp_root: Path, monkeypatch, capsys, case):
    """Which server a provider attributes `mcp__a__b__read` to is not established; settings and
    the Codex hook refuse the selection instead of granting it to both."""
    from agent_cli.dispatch import codex_permissions

    selected, rule, name = AMBIGUOUS[case]
    _set_alpha_dispatch(tmp_root, repo_mcp=selected, allowed_tools=[rule])
    servers = {server: {"command": sys.executable} for server in selected}
    (tmp_root / "repos/alpha-api/.mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    refusal = (
        f"repo_mcp selects {name}, whose name runs into the MCP rule delimiter `__`, so no "
        "mcp__<server>__<tool> rule says which server it grants; rename it or drop it from repo_mcp"
    )
    with pytest.raises(AgentError) as built:
        _built(tmp_root)
    assert str(built.value) == f"{BROKEN}{refusal}"
    with pytest.raises(AgentError):
        _hook(tmp_root, monkeypatch, rule)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_root))
    monkeypatch.setenv("AGENT_DISPATCH_ID", "999-99")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": rule, "tool_input": {}})))
    capsys.readouterr()
    assert codex_permissions.main() == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_prefix_servers_stay_separate_and_tool_suffixes_stay_legal(tmp_root: Path, monkeypatch):
    _set_alpha_dispatch(tmp_root, repo_mcp=["a", "ab"], allowed_tools=["mcp__a__read", "mcp__ab__write"])
    allow = _built(tmp_root)["permissions"]["allow"]
    assert [r for r in allow if r.startswith(("mcp__a__", "mcp__ab__"))] == ["mcp__a__read", "mcp__ab__write"]
    decisions = {
        "mcp__a__read": "allow",
        "mcp__a__write": "deny",
        "mcp__ab__read": "deny",
        "mcp__ab__write": "allow",
    }
    for tool, verdict in decisions.items():
        assert _hook(tmp_root, monkeypatch, tool)[0] == verdict, tool
    _set_alpha_dispatch(tmp_root, repo_mcp=["a"], allowed_tools=["mcp__a__b__read"])
    assert _hook(tmp_root, monkeypatch, "mcp__a__b__read")[0] == "allow"
