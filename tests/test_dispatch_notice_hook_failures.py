"""Dispatch notice hooks under failure: lost feedback, a held inbox
guard or session lock, failed saves, and arrivals around the acknowledgment.

Real hook entry points in temporary roots. Wall-clock checks only bound a held guard well under
the 5-second host budget; they do not measure precision.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest

from agent_cli import hooks, ops, peers, sessions
from agent_cli.dispatch import guards, notice_delivery, notices, verification_gate
from agent_cli.dispatch.state import load
from agent_cli.ops import peer_wake
from test_dispatch_notice_hooks import (
    BLOCKED,
    DID,
    HEADING,
    MGR,
    OTHER,
    PEER_HEADING,
    consumed,
    intent,
    mgr,  # noqa: F401 — the fixture
    notify,
    peer_send,
    prompt,
    queue,
    reservation,
    stop,
)

HOST_BUDGET_S = 5.0


@contextmanager
def held_elsewhere(guard: AbstractContextManager) -> Iterator[None]:
    """Hold `guard` on another thread until the block ends, or ten seconds at most."""
    taken, release = threading.Event(), threading.Event()

    def hold() -> None:
        with guard:
            taken.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    assert taken.wait(5)
    try:
        yield
    finally:
        release.set()
        worker.join()


def timed(call):
    began = time.monotonic()
    out = call()
    return out, time.monotonic() - began


def untouched(root: Path, nid: str, wake: dict, rows: list[str], *, undelivered: bool = False) -> None:
    """Nothing consumed, drained or acknowledged: every fallback stands. A wake whose read
    failed after taking the guard leaves only an undelivered marker, never a stale one."""
    assert consumed(root, nid) is None
    assert [i["envelope"]["notice_id"] for i in notice_delivery.pending(root, MGR)][-1] == nid
    assert [r["text"] for r in peers.peek(root, MGR)] == rows
    assert reservation(root) == wake
    marker = peers.stale_path(root, MGR)
    if undelivered:
        assert json.loads(marker.read_text()) == {
            "id": wake["id"],
            "watermark": wake["watermark"],
            "undelivered": True,
        }
    else:
        assert not marker.exists()
    assert load(root, DID)["chat_pending"] is True


# ------------------------------------------------------------------ C7: consumed, then feedback lost


def test_lost_stop_feedback_keeps_the_consumption_and_the_fallback(mgr: Path, monkeypatch):
    nid = notify(mgr)

    def crash(rows):
        raise RuntimeError("the hook died after the consumption save")

    with monkeypatch.context() as patch:
        patch.setattr(peers, "block", crash)
        assert stop(mgr) == ""  # the injection is lost
    assert consumed(mgr, nid) == "hook-consumed"
    assert stop(mgr) == ""  # and never blocks again, without a show or a reply
    assert load(mgr, DID)["chat_pending"] is True
    assert DID in json.loads(prompt(mgr, "owner's prompt"))["hookSpecificOutput"]["additionalContext"]


@pytest.fixture
def ringable(mgr: Path, monkeypatch):
    """The manager is a live Claude session, so delivery would ring it through a fake transport."""
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.setenv("AGENT_NOTIFY", "on")
    monkeypatch.setattr(ops.peers, "inbox_token", lambda address: "token")
    sessions.set_field(mgr, MGR, "pid", os.getpid())
    sessions.set_field(mgr, MGR, "inbox", str(mgr / "no-such.sock"))
    rings: list[str] = []

    def transport(target, name):
        rings.append(name)
        return True, "rang", None

    return rings, transport


def test_a_hook_consumed_notice_is_never_rung_again(mgr: Path, ringable):
    rings, transport = ringable
    nid = notify(mgr)
    assert json.loads(stop(mgr))["decision"] == "block"
    outcome = notice_delivery.deliver(mgr, DID, nid, transport=transport)
    assert rings == [] and outcome.failure == "stale"
    entry = load(mgr, DID)[notices.OUTBOX_FIELD][nid]
    assert entry["consumption"] == "hook-consumed"
    assert notice_delivery.eligible(load(mgr, DID)) == []
    assert stop(mgr) == ""


# ------------------------------------------------------------------ held guard and session lock


@pytest.mark.parametrize("hook", ["peer-stop", "manager-prompt", "dispatch-inbox"])
def test_a_held_inbox_guard_injects_nothing_well_inside_the_host_budget(mgr: Path, hook: str):
    nid = notify(mgr)
    peer_send(mgr, "queued")
    wake = reservation(mgr)
    payload = json.dumps({"session_id": MGR, "prompt": peer_wake.DISPATCH_WAKE_TEXT})
    with held_elsewhere(guards.area_guard(mgr, f"peer:{MGR}")):
        (out, _), elapsed = timed(lambda: hooks.run(mgr, hook, payload))
    assert elapsed < HOST_BUDGET_S - 1
    assert HEADING not in out and "decision" not in out
    untouched(mgr, nid, wake, ["queued"])
    assert json.loads(stop(mgr))["reason"].startswith(HEADING)  # once free, the fallback works


@pytest.mark.parametrize("hook", ["peer-stop", "dispatch-inbox", "manager-prompt", "codex-prompt"])
def test_a_held_session_lock_inside_the_guard_injects_nothing(mgr: Path, hook: str):
    """The acknowledgment's session lock is bounded by what is left of the one deadline, and a
    prompt hook's activity half by what is left of its own."""
    nid = notify(mgr)
    peer_send(mgr, "queued")
    wake = reservation(mgr)
    payload = json.dumps({"session_id": MGR, "prompt": peer_wake.DISPATCH_WAKE_TEXT})
    with held_elsewhere(sessions._locked(mgr, MGR)):
        (out, _), elapsed = timed(lambda: hooks.run(mgr, hook, payload))
    assert elapsed < HOST_BUDGET_S - 1
    assert HEADING not in out and "decision" not in out
    untouched(mgr, nid, wake, ["queued"], undelivered=True)
    reason = json.loads(stop(mgr))["reason"]
    assert HEADING in reason and "queued" in reason
    assert not peers.stale_path(mgr, MGR).exists()  # retired once the reservation is acknowledged


@pytest.mark.parametrize("hook", ["manager-prompt", "codex-prompt"])
@pytest.mark.parametrize("text", ["owner's prompt", peer_wake.DISPATCH_WAKE_TEXT])
def test_a_held_session_lock_ends_prompt_activity_and_nothing_writes_later(
    mgr: Path, capsys, hook: str, text: str
):
    sessions.set_field(mgr, MGR, "activity", {"state": "idle"})
    sessions.set_field(mgr, MGR, "owner_question_pending", True)
    nid = notify(mgr)
    payload = json.dumps({"session_id": MGR, "hook_event_name": "UserPromptSubmit", "prompt": text})
    path = sessions._path(mgr, MGR)
    with held_elsewhere(sessions._locked(mgr, MGR)):
        threads = set(threading.enumerate())
        (out, _), elapsed = timed(lambda: hooks.run(mgr, hook, payload))
        assert set(threading.enumerate()) == threads  # nothing left behind to write later
        held = path.read_bytes()
    assert elapsed < HOST_BUDGET_S - 1
    assert "decision" not in out
    assert "note: prompt activity failed" in capsys.readouterr().err
    with sessions._locked(mgr, MGR):  # the holder has released and joined; the lock is free
        assert path.read_bytes() == held
    record = sessions.get(mgr, MGR) or {}
    assert record["activity"]["state"] == "idle" and record["owner_question_pending"] is True
    assert consumed(mgr, nid) is None

    out = prompt(mgr, "owner's prompt", hook)  # uncontended, after the scope ended
    assert HEADING in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert (sessions.get(mgr, MGR) or {})["activity"]["state"] == "working"
    assert sessions._LOCK_DEADLINE.get() is None


def test_a_held_session_lock_defers_codex_resume_preparation(mgr: Path, monkeypatch):
    sessions.set_field(mgr, MGR, "provider", "codex")
    sessions.set_field(mgr, MGR, "pid", 11111)
    sessions.set_field(mgr, MGR, "tab_display_name", "mgr-one · cx 0001")
    monkeypatch.setattr(sessions, "resolve_pid", lambda: 22222)
    payload = json.dumps(
        {"session_id": MGR, "hook_event_name": "UserPromptSubmit", "prompt": "owner's prompt"}
    )
    with held_elsewhere(sessions._locked(mgr, MGR)):
        (out, _), elapsed = timed(lambda: hooks.run(mgr, "codex-prompt", payload))
    assert elapsed < HOST_BUDGET_S - 1
    record = sessions.get(mgr, MGR) or {}
    assert record["pid"] == 11111 and record["tab_display_name"] == "mgr-one · cx 0001"

    out = prompt(mgr, "owner's prompt", "codex-prompt")  # the next prompt prepares the resume
    assert "source: resume" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert (sessions.get(mgr, MGR) or {})["tab_display_name"] is None


def test_a_failed_acknowledgment_does_not_let_the_spent_wake_swallow_the_next_send(mgr: Path):
    """The wake that rang for these items delivered nothing. Without a marker, its accepted
    reservation would absorb every send for 30 minutes and an idle manager would never ring."""
    nid = notify(mgr)
    peer_send(mgr, "queued")
    with held_elsewhere(sessions._locked(mgr, MGR)):
        assert stop(mgr) == ""
    assert peer_send(mgr, "later") is not None  # a new reservation: this send rings
    assert consumed(mgr, nid) is None
    assert [r["text"] for r in peers.peek(mgr, MGR)] == ["queued", "later"]
    reason = json.loads(stop(mgr))["reason"]
    assert reason.startswith(HEADING + " (1)") and "queued" in reason and "later" in reason
    assert reservation(mgr) == {} and not peers.stale_path(mgr, MGR).exists()


def test_a_failed_save_after_the_acknowledgment_lets_the_next_send_ring(mgr: Path, monkeypatch):
    peer_send(mgr, "queued")  # rings, and the ring is accepted
    nid = notify(mgr)
    wake = reservation(mgr)
    assert wake["state"] == "accepted"
    with monkeypatch.context() as patch:
        _failing_store_write(patch)
        assert stop(mgr) == ""
    assert reservation(mgr) == wake  # put back, but marked so it absorbs nothing more
    assert peer_send(mgr, "later") is not None
    assert consumed(mgr, nid) is None
    assert [r["text"] for r in peers.peek(mgr, MGR)] == ["queued", "later"]


def test_an_ordinary_codex_prompt_that_fails_leaves_the_queued_wake_unmarked(mgr: Path, monkeypatch):
    """Codex has a queued wake prompt that will retry; marking it would ring a second time."""
    sessions.set_field(mgr, MGR, "provider", "codex")
    notify(mgr)
    _failing_store_write(monkeypatch)
    prompt(mgr, "owner's prompt", "codex-prompt")
    assert not peers.stale_path(mgr, MGR).exists()


def test_the_hook_takes_one_one_second_deadline(mgr: Path, monkeypatch):
    notify(mgr)
    seen: dict[str, float | None] = {}
    real_guard, real_ack = peers.inbox_guard, peers.acknowledge_observed

    def guard(root, sid, *, timeout=None):
        seen["guard"] = timeout
        return real_guard(root, sid, timeout=timeout)

    def ack(root, sid, observed, *, timeout=None):
        seen["ack"] = timeout
        return real_ack(root, sid, observed, timeout=timeout)

    monkeypatch.setattr(peers, "inbox_guard", guard)
    monkeypatch.setattr(peers, "acknowledge_observed", ack)
    assert json.loads(stop(mgr))["decision"] == "block"
    assert seen["guard"] == 1.0
    assert seen["ack"] is not None and 0 <= seen["ack"] <= 1.0  # what is left, not a fresh second


def test_a_spent_deadline_saves_no_consumption(mgr: Path, monkeypatch):
    from agent_cli.hooks import notice_inbox

    nid = notify(mgr)
    monkeypatch.setattr(notice_inbox, "HOOK_DEADLINE_S", 0.0)
    assert stop(mgr) == ""
    assert consumed(mgr, nid) is None


# ------------------------------------------------------------------ failed saves


def _failing_store_write(monkeypatch) -> None:
    real = notice_delivery.atomic_write

    def write(path, text):
        if notice_delivery.STORE_DIR.name in str(path):
            raise OSError("disk full")
        return real(path, text)

    monkeypatch.setattr(notice_delivery, "atomic_write", write)


@pytest.mark.parametrize("hook", ["peer-stop", "dispatch-inbox"])
def test_a_failed_consumption_save_injects_nothing_and_drains_no_peer(mgr: Path, monkeypatch, hook: str):
    peer_send(mgr, "queued")
    first, second = notify(mgr), notify(mgr, BLOCKED, OTHER)
    wake = reservation(mgr)
    payload = json.dumps({"session_id": MGR, "prompt": peer_wake.DISPATCH_WAKE_TEXT})
    with monkeypatch.context() as patch:
        _failing_store_write(patch)
        out, _ = hooks.run(mgr, hook, payload)
    assert HEADING not in out and PEER_HEADING not in out and "decision" not in out
    assert consumed(mgr, first) is None and consumed(mgr, second) is None
    untouched(mgr, second, wake, ["queued"], undelivered=True)
    reason = json.loads(stop(mgr))["reason"]
    assert reason.startswith(HEADING + " (2)") and "queued" in reason


def test_a_failed_peer_drain_after_consumption_keeps_both(mgr: Path, monkeypatch):
    """The notices are consumed, so they are shown; the peer rows stay queued for next time."""
    peer_send(mgr, "queued")
    nid = notify(mgr)
    real = peers.atomic_write

    def write(path, text):
        if str(path).endswith(".jsonl"):
            raise OSError("disk full")
        return real(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(peers, "atomic_write", write)
        reason = json.loads(stop(mgr))["reason"]
    assert reason.startswith(HEADING) and PEER_HEADING not in reason
    assert consumed(mgr, nid) == "hook-consumed"
    assert [r["text"] for r in peers.peek(mgr, MGR)] == ["queued"]
    reason = json.loads(stop(mgr))["reason"]
    assert reason.startswith(PEER_HEADING) and "queued" in reason


def test_an_unreadable_notice_store_still_delivers_peer_messages(mgr: Path):
    notice_delivery.store_path(mgr, MGR).parent.mkdir(parents=True, exist_ok=True)
    notice_delivery.store_path(mgr, MGR).write_text("{not json")
    peer_send(mgr, "queued")
    reason = json.loads(stop(mgr))["reason"]
    assert reason.startswith(PEER_HEADING) and "queued" in reason


def test_an_unreadable_source_record_is_not_notice_context(mgr: Path):
    nid = notify(mgr)
    (mgr / ".agent" / "dispatches" / f"{DID}.json").write_text("{not json")
    assert json.loads(prompt(mgr, peer_wake.DISPATCH_WAKE_TEXT))["decision"] == "block"
    assert consumed(mgr, nid) is None


# ------------------------------------------------------------------ arrivals around the acknowledgment


def test_an_arrival_after_the_acknowledgment_rings_and_blocks_once(mgr: Path):
    first = notify(mgr)
    assert json.loads(stop(mgr))["decision"] == "block"
    assert reservation(mgr) == {}
    later = intent(mgr, BLOCKED)
    assert queue(mgr, later) is not None  # a fresh reservation: this arrival rings
    assert consumed(mgr, first) == "hook-consumed" and consumed(mgr, later) is None
    reason = json.loads(stop(mgr))["reason"]
    assert "state blocked" in reason and "state done" not in reason


def test_an_extended_reservation_is_not_cleared_by_an_older_observation(mgr: Path, monkeypatch):
    """A sender that reserved after the hook observed would extend the watermark; the hook's
    acknowledgment must leave that reservation standing for the later arrival."""
    nid = notify(mgr)
    real = peers.observe_wake

    def observe_then_arrive(root, sid):
        seen = real(root, sid)
        current = reservation(root)
        sessions.set_field(root, sid, peers.WAKE_FIELD, {**current, "watermark": seen["watermark"] + 1})
        return seen

    monkeypatch.setattr(peers, "observe_wake", observe_then_arrive)
    assert json.loads(stop(mgr))["decision"] == "block"
    assert consumed(mgr, nid) == "hook-consumed"
    assert reservation(mgr)["watermark"] == 2  # still covers the arrival this hook never saw


def test_no_dispatch_record_guard_is_taken_inside_the_inbox_guard(mgr: Path, monkeypatch):
    notify(mgr)
    held: list[str] = []
    overlaps: list[str] = []
    real = guards.area_guard

    @contextmanager
    def tracking(root, area, **kwargs):
        with real(root, area, **kwargs):
            if any(a.startswith("peer:") != area.startswith("peer:") for a in held):
                overlaps.append(area)
            held.append(area)
            try:
                yield
            finally:
                held.pop()

    monkeypatch.setattr(guards, "area_guard", tracking)
    monkeypatch.setattr(verification_gate, "area_guard", tracking)
    assert json.loads(stop(mgr))["decision"] == "block"
    assert held == [] and overlaps == []
