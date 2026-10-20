"""A help suffix must not become a sandbox command payload."""

import json

from hooks_test_helpers import _decision


def _bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def test_sandbox_help_after_payload_is_denied(run):
    for command in ("codex sandbox macos --help", "codex sandbox -C /tmp macos --help"):
        assert _decision(run(["hook", "sandbox-help-guard"], stdin=_bash(command))) == "deny"
    for command in ("codex sandbox --help", "codex sandbox -C /tmp --help"):
        assert run(["hook", "sandbox-help-guard"], stdin=_bash(command)).out == ""
