"""`cli-route`: models never run `bin/agent` in a shell; the refusal names the MCP tool."""

from __future__ import annotations

import json

import pytest

from agent_cli.hooks.run import run


def _decide(root, command: str) -> dict | None:
    out, code = run(root, "cli-route", json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    assert code == 0
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.mark.parametrize(
    ("command", "names"),
    [
        ('bin/agent peer send mgr-x "hello | world"', "`peer` with action `send`"),
        ("bin/agent --json task done 3", "`task_done`"),
        ("./bin/agent log 052 note hi", "`log`"),
        ("/Users/x/Workspace/Agent/bin/agent status render", "`status_render`"),
        (".venv/bin/agent repos sync --pull", "`repos_sync`"),
        # Reads route to the read-only `dispatch_read` tool
        ("uv run agent dispatch show 001-54", "`dispatch_read` with action `show`"),
        ("bin/agent verify show HEAD", "`harness` with action `verify_show`"),
        ("bin/agent doctor 2>&1 | tail -20", "`harness` with action `doctor`"),
        ("bin/agent --json journal collect 2026-09-30 --write", "`harness` with action `journal_collect`"),
        ("bin/agent verify record HEAD", "`harness` with action `verify_record`"),
        ("AGENT_X=1 bin/agent notes root", "`harness` with action `notes_root`"),
        ("bash -c 'bin/agent lease list'", "`lease` with action `list`"),
        ("bin/agent.cmd peer inbox", "`peer` with action `inbox`"),
        ("env -i bin/agent peer inbox", "`peer` with action `inbox`"),
        ("env -u HOME PATH=/x bin/agent log 052 note x", "`log`"),
        ("uv run --directory . agent task list", "`task_list`"),
        ("uv run --with rich agent doctor", "`harness` with action `doctor`"),
        ("command bin/agent repos list", "`repos_list`"),
        ("bash -lc 'bin/agent peer inbox'", "`peer` with action `inbox`"),
        ("zsh -ic 'cd x; bin/agent task add y'", "`task_add`"),
        ("/bin/sh -x -c 'bin/agent feature current'", "`feature_show`"),
    ],
)
def test_mcp_covered_actions_are_refused_with_the_tool_named(tmp_root, command, names):
    decision = _decide(tmp_root, command)
    assert decision is not None and decision["permissionDecision"] == "deny"
    assert names in decision["permissionDecisionReason"]
    assert "not fall back" in decision["permissionDecisionReason"]


def test_person_only_actions_are_refused_with_a_handoff(tmp_root):
    decision = _decide(tmp_root, "bin/agent export -m 'x'")
    assert decision is not None and decision["permissionDecision"] == "deny"
    assert "person-only" in decision["permissionDecisionReason"]


def test_help_and_bare_calls_are_refused(tmp_root):
    for command in ("bin/agent --help", "bin/agent", "bin/agent peers list"):
        decision = _decide(tmp_root, command)
        assert decision is not None and decision["permissionDecision"] == "deny", command


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn 'bin/agent peer send' src",
        "rg bin/agent",
        'git commit -m "stop using bin/agent log"',
        "node -e \"require('x')\" agent hook cli-route",
        "ls bin/",
        "uv run pytest tests/test_cli_route.py",
        "echo agent",
    ],
)
def test_other_commands_pass(tmp_root, command):
    assert _decide(tmp_root, command) is None


def test_other_tools_pass(tmp_root):
    payload = {"tool_name": "Edit", "tool_input": {"command": "bin/agent log"}}
    out, _ = run(tmp_root, "cli-route", json.dumps(payload))
    assert out == ""
