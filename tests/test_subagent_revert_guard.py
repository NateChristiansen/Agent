"""Prose assignments cannot authorize a subagent's destructive revert."""

import json

from hooks_test_helpers import _decision


def _bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def test_subagent_revert_is_denied_before_files_change(run, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT_ID", "agent-child")
    for command in ("git checkout -- src/other.py", "git -C /tmp/repo restore src/other.py"):
        assert _decision(run(["hook", "subagent-revert-guard"], stdin=_bash(command))) == "deny"
    assert run(["hook", "subagent-revert-guard"], stdin=_bash("git checkout feature/branch")).out == ""

    monkeypatch.delenv("CLAUDE_AGENT_ID")
    assert run(["hook", "subagent-revert-guard"], stdin=_bash("git checkout -- src/other.py")).out == ""
