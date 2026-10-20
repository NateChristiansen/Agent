"""How the shared guards behave on Codex payloads.

Split from `test_hooks_guards.py`, which hit the 500-line limit. The concern is its own:
the guards are written once and wired into both clients, so what differs is not the rule
but what each client can express. Codex parses `permissionDecision: "ask"` and does not
support it — the hook fails and the call proceeds — so an `ask` that reads as a prompt on
Claude is an open door there.
"""

from __future__ import annotations

from pathlib import Path


def test_no_guard_answers_ask_for_a_codex_shaped_payload(tmp_root: Path):
    """Codex parses `permissionDecision: "ask"` and does not support it: the hook fails and the
    call proceeds. So a guard that would prompt the owner on Claude is an open door on Codex,
    which is worse than having no guard, because it reads as one.

    This case exercises Bash payloads only. Codex also has `apply_patch` file edits, which
    require a separate matcher. The Bash branch never asks. This pins it: if a future guard
    adds an `ask` on the Bash path it has to answer for Codex first.
    """
    import json

    from agent_cli import hooks

    payloads = [
        {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
        {"tool_name": "Bash", "tool_input": {"command": "echo hi > STATUS.md"}},
        {"tool_name": "Bash", "tool_input": {"command": "cat /etc/passwd"}},
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}},
    ]
    for name in ("protect-generated", "no-chaining", "push-guard"):
        handler = hooks.HANDLERS[name]
        for payload in payloads:
            out = handler(tmp_root, dict(payload))
            if not out:
                continue
            decision = json.loads(out).get("hookSpecificOutput", {}).get("permissionDecision")
            assert decision != "ask", f"{name} asked on a Codex-reachable payload: {payload}"


def test_bash_only_hook_does_not_see_apply_patch(tmp_root: Path):
    """A Bash matcher cannot guard a Codex file edit."""
    import json

    from agent_cli import hooks
    from agent_cli.hooks.groups import wired

    config = json.loads((Path(__file__).resolve().parents[1] / ".codex/hooks.json").read_text())
    assert "pattern-kill-guard" in wired(config, "PreToolUse", "Bash")
    assert "pattern-kill-guard" not in wired(config, "PreToolUse", "apply_patch")
    patch = {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** End Patch"}}
    assert hooks.HANDLERS["pattern-kill-guard"](tmp_root, patch) == ""
