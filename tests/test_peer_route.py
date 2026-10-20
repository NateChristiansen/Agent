"""A manager is reached with `peer send`, not SendMessage.

On 2026-09-25 a Claude manager used SendMessage for a Codex manager, got "No agent named ... is
reachable", and asked the owner for a hand relay.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli import hooks, sessions


def _decide(root: Path, tool: str, tool_input: object = None) -> str:
    payload = {"tool_name": tool, "tool_input": {} if tool_input is None else tool_input}
    out, _ = hooks.run(root, "plan-mode-allow", json.dumps(payload))
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else ""


def test_peer_and_mapping_tools_are_allowed_and_nothing_else_is(tmp_root: Path):
    """052: plan mode asked before every peer call, and kept a manager from mapping itself."""
    for tool in ("peer", "feature_use", "feature_show", "session_get"):
        assert _decide(tmp_root, f"mcp__agent__{tool}") == "allow", tool
    for other in ("mcp__agent__feature_new", "mcp__agent__harness", "mcp__agent__task_done", "Bash"):
        assert _decide(tmp_root, other) == "", other


def test_only_the_plan_review_dispatch_is_allowed_in_plan_mode(tmp_root: Path):
    """A plan's review launches from inside plan mode; no other dispatch action does."""
    assert _decide(tmp_root, "mcp__agent__dispatch", {"action": "plan-review", "paths": ["p.md"]}) == "allow"
    for action in ("new", "resume", "say", "cancel", "collect"):
        assert _decide(tmp_root, "mcp__agent__dispatch", {"action": action}) == "", action
    assert _decide(tmp_root, "mcp__agent__dispatch") == ""
    assert _decide(tmp_root, "mcp__agent__dispatch", "plan-review") == ""  # malformed input


def test_plan_mode_allow_is_wired_on_the_same_tools():
    from agent_cli.hooks.groups import wired
    from agent_cli.hooks.peer_route import PLAN_MODE_DISPATCH, PLAN_MODE_TOOLS

    config = json.loads((Path(__file__).resolve().parents[1] / ".claude/settings.json").read_text())
    for tool in (*PLAN_MODE_TOOLS, PLAN_MODE_DISPATCH[0]):
        assert wired(config, "PreToolUse", tool).count("plan-mode-allow") == 1, tool
    assert "plan-mode-allow" not in wired(config, "PreToolUse", "mcp__agent__feature_new")


def _send(root: Path, to: str) -> str:
    payload = {"session_id": "me", "tool_name": "SendMessage", "tool_input": {"to": to, "message": "hi"}}
    out, code = hooks.run(root, "peer-route", json.dumps(payload))
    assert code == 0
    return out


def test_sendmessage_to_a_manager_is_denied_with_the_peer_command(tmp_root: Path):
    sessions.set_record(tmp_root, "01a0d8aa-codex-session", name="mgr-unmapped-01a0d8-codex-bfe53a44")
    for target in ("mgr-unmapped-01a0d8-codex-bfe53a44", "01a0d8aa-codex-session", "01a0d8aa"):
        out = _send(tmp_root, target)
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", target
        assert (
            "the `peer` MCP tool (action send, to mgr-unmapped-01a0d8-codex-bfe53a44)"
            in decision["permissionDecisionReason"]
        )


def test_sendmessage_to_a_worker_or_subagent_is_allowed(tmp_root: Path):
    sessions.set_record(tmp_root, "worker-session", name="001-41", dispatch_id="001-41")
    assert _send(tmp_root, "001-41") == ""
    assert _send(tmp_root, "a13db6a9fa19fa19e") == ""  # a subagent id, no session record


def test_sendmessage_is_wired_on_claude():
    from agent_cli.hooks.groups import wired

    config = json.loads((Path(__file__).resolve().parents[1] / ".claude/settings.json").read_text())
    assert wired(config, "PreToolUse", "SendMessage") == ["peer-route"]
