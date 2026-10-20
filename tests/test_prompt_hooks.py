"""Inbox delivery and prompt activity must share one ordered hook for each manager client."""

import json
from pathlib import Path

import pytest

from agent_cli import ops, peers, sessions
from agent_cli.hooks.activity import activity, codex_activity
from agent_cli.ops.peer_wake import DISPATCH_WAKE_TEXT


@pytest.mark.parametrize("activity_hook", [activity, codex_activity])
def test_old_prompt_wiring_ignores_peer_wake_until_real_tool_use(tmp_root: Path, activity_hook):
    """Already-running clients keep their original separate hook group until restart."""
    sessions.set_record(tmp_root, "s-me", pid=424243, name="mgr-me")
    sessions.set_field(tmp_root, "s-me", "activity", {"state": "idle"})
    sessions.set_field(tmp_root, "s-me", "owner_question_pending", True)
    payload = {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": ops.peers.WAKE_TEXT}
    activity_hook(tmp_root, payload)
    record = sessions.get(tmp_root, "s-me") or {}
    assert record["activity"]["state"] == "idle"
    assert record["owner_question_pending"] is True

    activity_hook(tmp_root, {"session_id": "s-me", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "working"

    activity_hook(tmp_root, {"session_id": "s-me", "hook_event_name": "Stop"})
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "idle"
    activity_hook(tmp_root, payload)  # no-tool wake turn, then Stop
    activity_hook(tmp_root, {"session_id": "s-me", "hook_event_name": "Stop"})
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "idle"

    activity_hook(
        tmp_root,
        {"session_id": "s-me", "agent_id": "child", "hook_event_name": "PostToolUse", "tool_name": "Bash"},
    )
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "idle"

    sessions.set_field(tmp_root, "s-me", "activity", {"state": "ended"})
    activity_hook(tmp_root, {"session_id": "s-me", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "ended"


@pytest.mark.parametrize("hook", ["manager-prompt", "codex-prompt"])
def test_stale_wake_does_not_start_activity_without_a_turn(tmp_root: Path, run, hook: str):
    """A blocked wake has no Stop. Capture still records its text, but cannot change activity."""
    sessions.set_record(tmp_root, "s-me", pid=424243, name="mgr-me")
    for state in ("idle", "waiting_on_you", "working"):
        sessions.set_field(tmp_root, "s-me", "activity", {"state": state})
        if state == "waiting_on_you":
            sessions.set_field(tmp_root, "s-me", "owner_question_pending", True)
        result = run(
            ["hook", hook],
            stdin=json.dumps(
                {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": ops.peers.WAKE_TEXT}
            ),
        )
        assert json.loads(result.out) == {"decision": "block", "reason": "Peer wake already delivered."}
        record = sessions.get(tmp_root, "s-me") or {}
        assert record["activity"]["state"] == state
        if state == "waiting_on_you":
            assert record["owner_question_pending"] is True


@pytest.mark.parametrize("hook", ["manager-prompt", "codex-prompt"])
def test_delivered_wake_starts_activity_at_tool_result_and_owner_prompt_at_submit(
    tmp_root: Path, run, hook: str
):
    sessions.set_record(tmp_root, "s-me", pid=424243, name="mgr-me")
    sessions.set_field(tmp_root, "s-me", "activity", {"state": "idle"})
    peers.send(tmp_root, to="s-me", text="review ready", frm="s-you")
    wake = run(
        ["hook", hook],
        stdin=json.dumps(
            {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": ops.peers.WAKE_TEXT}
        ),
    )
    assert "review ready" in json.loads(wake.out)["hookSpecificOutput"]["additionalContext"]
    # A wake that reaches activity was accepted by the inbox and starts a turn, so
    # the tab reads working from the prompt, not from the turn's first tool result.
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "working"
    activity_hook = codex_activity if hook == "codex-prompt" else activity
    activity_hook(tmp_root, {"session_id": "s-me", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "working"

    sessions.set_field(tmp_root, "s-me", "activity", {"state": "idle"})
    owner = run(
        ["hook", hook],
        stdin=json.dumps(
            {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": "owner prompt"}
        ),
    )
    assert owner.out == ""
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "working"


@pytest.mark.parametrize(
    ("settings", "combined", "separate"),
    [
        (".claude/settings.json", "manager-prompt", "activity"),
        (".codex/hooks.json", "codex-prompt", "codex-activity"),
    ],
)
def test_prompt_wiring_runs_inbox_and_activity_once(settings: str, combined: str, separate: str):
    from agent_cli.hooks.groups import wired

    path = Path(__file__).resolve().parents[1] / settings
    names = wired(json.loads(path.read_text()), "UserPromptSubmit")
    assert names.count(combined) == 1
    assert "dispatch-inbox" not in names and separate not in names


@pytest.mark.parametrize(
    ("hook", "handler"),
    [("manager-prompt", "activity"), ("codex-prompt", "codex_activity")],
)
def test_prompt_delivers_inbox_when_activity_fails(tmp_root: Path, run, monkeypatch, hook: str, handler: str):
    peers.send(tmp_root, to="s-me", text="do not lose this", frm="s-you")

    def broken_activity(root: Path, payload: dict) -> str:
        raise RuntimeError("activity unavailable")

    monkeypatch.setattr(f"agent_cli.hooks.prompt.{handler}", broken_activity)
    result = run(
        ["hook", hook],
        stdin=json.dumps(
            {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": "owner prompt"}
        ),
    )
    assert "do not lose this" in json.loads(result.out)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("activity_hook", [activity, codex_activity])
@pytest.mark.parametrize(
    "wake",
    [ops.peers.WAKE_TEXT, DISPATCH_WAKE_TEXT, " \n\t" + ops.peers.WAKE_TEXT, DISPATCH_WAKE_TEXT + "\n"],
)
def test_a_machine_wake_never_answers_the_owner_question(tmp_root: Path, activity_hook, wake: str):
    """A dispatch notice used to clear the hand as if the owner had replied."""
    sessions.set_record(tmp_root, "s-me", pid=424243, name="mgr-me")
    sessions.set_field(tmp_root, "s-me", "activity", {"state": "idle"})
    sessions.set_field(tmp_root, "s-me", "owner_question_pending", True)
    sessions.set_field(tmp_root, "s-me", "owner_question_id", "q-1")
    activity_hook(tmp_root, {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": wake})
    record = sessions.get(tmp_root, "s-me") or {}
    assert record["owner_question_pending"] is True
    assert record["owner_question_id"] == "q-1"

    activity_hook(tmp_root, {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": "yes"})
    assert not (sessions.get(tmp_root, "s-me") or {}).get("owner_question_pending")


@pytest.mark.parametrize("activity_hook", [activity, codex_activity])
def test_an_accepted_dispatch_wake_starts_working_and_an_unflagged_one_does_not(
    tmp_root: Path, activity_hook
):
    sessions.set_record(tmp_root, "s-me", pid=424243, name="mgr-me")
    sessions.set_field(tmp_root, "s-me", "activity", {"state": "idle"})
    payload = {"session_id": "s-me", "hook_event_name": "UserPromptSubmit", "prompt": DISPATCH_WAKE_TEXT}
    activity_hook(tmp_root, payload)  # the old separate wiring: no inbox verdict, so no change
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "idle"
    activity_hook(tmp_root, {**payload, "_wake_accepted": True})
    assert (sessions.get(tmp_root, "s-me") or {})["activity"]["state"] == "working"
