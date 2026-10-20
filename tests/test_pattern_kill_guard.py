"""A pattern kill in one manager session must not reach another session."""

import json

from hooks_test_helpers import _decision


def _bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def test_pattern_kill_is_denied_but_exact_pid_kill_is_allowed(run):
    for command in (
        "pkill -f codex",
        "killall Terminal",
        "sudo pkill -f agent",
        "sudo -n pkill -f agent",
        "sh -c 'pkill -f agent'",
        "env FOO=1 /usr/bin/killall python",
    ):
        assert _decision(run(["hook", "pattern-kill-guard"], stdin=_bash(command))) == "deny"
    for command in ("kill 12345", "rg pkill src", "git log --grep killall"):
        assert run(["hook", "pattern-kill-guard"], stdin=_bash(command)).out == ""
