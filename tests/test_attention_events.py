"""The events nobody could find in the logs (phase H): why a hand went up, whether a peer
message was queued, rung and read, and closes, tasks and journals. Ids and causes only."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_cli import ops, peers, sessions
from agent_cli.hooks import notice_inbox
from agent_cli.ops import peer_wake
from agent_cli.ops.peer_delivery import deliver
from agent_cli.ops.peer_wake import DISPATCH_WAKE_TEXT
from agent_cli.telemetry import recorder as recorder_mod
from agent_cli.telemetry import schema
from agent_cli.telemetry.sink import Sink

activity_mod = importlib.import_module("agent_cli.hooks.activity")
NEW_KINDS = (
    "activity.transition",
    "permission.request",
    "peer.enqueue",
    "peer.ring",
    "peer.consume",
    "feature.close",
    "task.add",
    "task.done",
    "journal.write",
)


@pytest.fixture(autouse=True)
def quiet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loss = tmp_path / "losstmp"
    loss.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(loss))
    monkeypatch.setattr(recorder_mod, "_process", None)
    monkeypatch.setattr(activity_mod, "refresh_tab", lambda *a, **k: None)
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)


def events(root: Path, kind: str) -> list[dict[str, Any]]:
    files = Sink.for_main(recorder_mod.main_of(root)).events
    lines = [line for p in sorted(files.glob("*.jsonl")) for line in p.read_text().splitlines()]
    return [e for e in map(json.loads, lines) if e["kind"] == kind]


def test_the_new_kinds_are_known_to_the_schema():
    for kind in NEW_KINDS:
        assert schema.known_kind(kind), kind


def test_a_permission_hand_and_a_wake_turn_carry_their_cause(tmp_root: Path):
    sessions.set_record(tmp_root, "s1", pid=os.getpid(), name="mgr-s1")
    sessions.set_field(tmp_root, "s1", "activity", {"state": "working"})
    activity_mod.activity(
        tmp_root, {"session_id": "s1", "hook_event_name": "PermissionRequest", "tool_name": "Bash"}
    )
    assert [e["data"] for e in events(tmp_root, "permission.request")] == [{"tool": "Bash"}]
    activity_mod.activity(tmp_root, {"session_id": "s1", "hook_event_name": "Stop"})
    activity_mod.activity(
        tmp_root,
        {
            "session_id": "s1",
            "hook_event_name": "UserPromptSubmit",
            "prompt": DISPATCH_WAKE_TEXT,
            "_wake_accepted": True,
        },
    )
    transitions = [e["data"] for e in events(tmp_root, "activity.transition")]
    assert transitions == [
        {"from": "working", "to": "waiting_on_you", "cause": "PermissionRequest:Bash"},
        {"from": "waiting_on_you", "to": "idle", "cause": "Stop"},
        {"from": "idle", "to": "working", "cause": "dispatch_wake"},
    ]
    activity_mod.activity(
        tmp_root, {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash"}
    )
    assert len(events(tmp_root, "activity.transition")) == 3  # unchanged state: no event
    text = "".join(
        p.read_text() for p in Sink.for_main(recorder_mod.main_of(tmp_root)).events.glob("*.jsonl")
    )
    assert "Dispatch supervisor notice" not in text


@pytest.mark.parametrize("provider", ["claude", "codex", None])
def test_permission_and_transition_keep_the_session_provider(tmp_root: Path, provider):
    sessions.set_record(tmp_root, "s1", name="mgr-s1", pid=os.getpid())
    if provider:
        sessions.set_field(tmp_root, "s1", "provider", provider)
    activity_mod.activity(
        tmp_root, {"session_id": "s1", "hook_event_name": "PermissionRequest", "tool_name": "Bash"}
    )
    for kind in ("permission.request", "activity.transition"):
        (row,) = events(tmp_root, kind)
        assert row["provider"] == provider


def _live(root: Path, *sids: str) -> None:
    for sid in sids:
        sessions.set_record(root, sid, pid=os.getpid(), name=f"mgr-{sid}")
        sessions.set_field(root, sid, "inbox", f"/tmp/inbox-{sid}.sock")  # noqa: S108 — never opened


def test_enqueue_ring_and_consume_join_on_one_message_id(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    _live(tmp_root, "a", "b")
    outcomes: list[str | None] = ["connection refused", None]
    monkeypatch.setattr("agent_cli.ops.peers._ring_socket", lambda path: outcomes.pop(0))
    first = deliver(tmp_root, target="b", frm="a", message="Agreed to plan.md")
    retry = deliver(tmp_root, target="b", frm="a", message="Agreed to plan.md", notice=first.notice())
    assert (first.rang, retry.rang) == ("refused", "accepted") and first.id == retry.id
    rows, _ = notice_inbox.take(tmp_root, "b", acknowledge_wake=True, via="prompt")
    assert [r["id"] for r in rows] == [first.id]

    (enqueued,) = events(tmp_root, "peer.enqueue")
    assert enqueued["data"] == {"id": first.id, "to": "b", "from": "a", "origin": "local"}
    rings = [e["data"] for e in events(tmp_root, "peer.ring")]
    assert [(r["outcome"], r["retry"], r["message_ids"]) for r in rings] == [
        ("refused", False, [first.id]),
        ("accepted", True, [first.id]),
    ]
    (consumed,) = events(tmp_root, "peer.consume")
    assert consumed["data"]["ids"] == [first.id] and consumed["data"]["via"] == "prompt"
    assert consumed["data"]["wake_id"] == rings[1]["wake_id"]


def test_a_peek_then_a_take_consumes_once_and_an_unread_row_has_no_consume(tmp_root: Path, monkeypatch):
    _live(tmp_root, "a", "b", "c")
    monkeypatch.setattr("agent_cli.ops.peers._ring_socket", lambda path: None)
    deliver(tmp_root, target="b", frm="a", message="Agreed to x")
    deliver(tmp_root, target="c", frm="a", message="Agreed to y")
    ops.peer_inbox(tmp_root, session_id="b", peek=True)
    assert events(tmp_root, "peer.consume") == []
    ops.peer_inbox(tmp_root, session_id="b")
    consumed = events(tmp_root, "peer.consume")
    assert len(consumed) == 1 and consumed[0]["data"]["to"] == "b" and consumed[0]["data"]["via"] == "inbox"
    assert len(events(tmp_root, "peer.enqueue")) == 2 and len(events(tmp_root, "peer.ring")) == 2


def test_two_messages_sharing_a_coalesced_wake_both_join_to_it(tmp_root: Path, monkeypatch):
    _live(tmp_root, "a", "b")
    with peers.inbox_guard(tmp_root, "b"):
        wake = peer_wake.reserve(tmp_root, "b")
    one = deliver(tmp_root, target="b", frm="a", message="Agreed to x")
    two = deliver(tmp_root, target="b", frm="a", message="Agreed to y")
    rings = [e["data"] for e in events(tmp_root, "peer.ring")]
    assert {r["wake_id"] for r in rings} == {wake}
    assert [r["message_ids"] for r in rings] == [[one.id], [two.id]]
    assert {r["outcome"] for r in rings} == {"coalesced_pending"}


def test_a_drain_over_the_list_cap_is_split_never_lost(tmp_root: Path):
    """Codex review: the schema caps a list at 32, and a 33-message drain recorded nothing."""
    sent = [peers.send(tmp_root, to="b", text=f"m{i}", frm="a")["id"] for i in range(33)]
    peers.take(tmp_root, "b", via="inbox")
    consumed = [i for e in events(tmp_root, "peer.consume") for i in e["data"]["ids"]]
    assert consumed == sent


def test_remote_ingress_is_marked_remote(tmp_root: Path):
    peers.send(tmp_root, to="b", text="hello", frm="win-a", remote={"device": "win-box", "msg_id": "m1"})
    assert events(tmp_root, "peer.enqueue")[0]["data"]["origin"] == "remote"


def test_close_tasks_and_journal_are_recorded(tmp_root: Path, notes_remote: Path, run):
    from datetime import date

    run(["feature", "new", "x", "--title", "X"])
    ops.task_add(tmp_root, text="one", feature="1")
    ops.task_done(tmp_root, task="1", feature="1")
    assert [e["data"] for e in events(tmp_root, "task.add")] == [{"index": 1}]
    assert [e["data"] for e in events(tmp_root, "task.done")] == [{"index": 1}]
    assert events(tmp_root, "task.add")[0]["feature"] == "001"
    ops.feature_close(tmp_root, feature="1")
    assert [e["data"] for e in events(tmp_root, "feature.close")] == [{"status": "done", "retro": "missing"}]
    draft = tmp_root / "draft.md"
    draft.write_text("- did a thing\n", encoding="utf-8")
    ops.journal_write(tmp_root, date=date(2026, 9, 3).isoformat(), file=str(draft))
    assert [e["data"] for e in events(tmp_root, "journal.write")] == [{"date": "2026-09-03"}]
