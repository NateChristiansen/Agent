"""Delivering a dispatch notice: claims, the recipient store, routes, suppression and the shared
wake reservation. Fake transports and a fake clock; nothing rings a real
session or writes outside the temporary root."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import ops, peers, sessions
from agent_cli.core import AgentError, now
from agent_cli.dispatch import guards, notice_delivery, notices, verification_gate
from agent_cli.dispatch.state import load, save
from agent_cli.dispatch.verification_gate import record_guard
from agent_cli.ops import peer_wake
from notice_helpers import held_elsewhere as _held_elsewhere
from notice_helpers import peer_send as _peer_send

DID = "001-mac-01"
MGR = "mgr-1"
DONE = notices.Disposition(1, "terminal", "done", "verified", "a" * 32, False, None)
BLOCKED = notices.Disposition(1, "terminal", "blocked", "unverified", None, False, None)
RECORD = {"short": "agent", "repo": "Agent", "feature": 23, "state": "done", "attempt": 1, "notify": ["chat"]}


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def live(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    """Rings on, a live Claude launcher with an inbox key, guards and transport recorded."""
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.setenv("AGENT_NOTIFY", "on")
    monkeypatch.setattr(ops.peers, "inbox_token", lambda address: "token")
    monkeypatch.setattr(ops.peers, "_ring_socket", _no_real_ring)
    monkeypatch.setattr(ops.peers, "_run", _no_real_ring)
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["codex", *args])
    sessions.set_record(tmp_root, MGR, name="mgr-one", pid=os.getpid())
    sessions.set_field(tmp_root, MGR, "provider", "claude")
    sessions.set_field(tmp_root, MGR, "inbox", str(tmp_root / "no-such.sock"))

    held: list[str] = []
    overlaps: list[tuple[tuple[str, ...], str]] = []
    real = guards.area_guard

    @contextmanager
    def tracking(root, area, **kwargs):
        with real(root, area, **kwargs):
            if any(a.startswith("peer:") != area.startswith("peer:") for a in held):
                overlaps.append((tuple(held), area))
            held.append(area)
            try:
                yield
            finally:
                held.pop()

    monkeypatch.setattr(guards, "area_guard", tracking)
    monkeypatch.setattr(verification_gate, "area_guard", tracking)

    rings: list[tuple[str, tuple[str, ...]]] = []

    def transport(target, name):
        rings.append((str(target["session_id"]), tuple(held)))
        assert notice_delivery.pending(tmp_root, MGR), "the notice is appended before the ring"
        assert (sessions.get(tmp_root, MGR) or {}).get(peers.WAKE_FIELD), "and the reservation taken"
        return True, "rang", None

    save(tmp_root, {**RECORD, "id": DID})
    return SimpleNamespace(
        root=tmp_root, clock=Clock(), rings=rings, transport=transport, held=held, overlaps=overlaps
    )


def _no_real_ring(*args, **kwargs):
    raise AssertionError("a test reached a real transport")


def _intent(root: Path, disposition: notices.Disposition = DONE, session: str = MGR, did: str = DID) -> str:
    harness = notices.harness_identity(root)
    with record_guard(root, did) as d:
        entry = notices.record_intent(
            d, disposition, harness=harness, launch_route=notices.route(harness, session)
        )
        save(root, d)
    return entry["notice_id"]


def _entry(root: Path, nid: str) -> dict:
    return load(root, DID)[notices.OUTBOX_FIELD][nid]


def _deliver(env, nid: str, **kwargs):
    kwargs.setdefault("transport", env.transport)
    return notice_delivery.deliver(env.root, DID, nid, clock=env.clock, **kwargs)


def _reservation(root: Path, sid: str = MGR) -> dict:
    return (sessions.get(root, sid) or {}).get(peers.WAKE_FIELD) or {}


# ------------------------------------------------------------------ append, ring, settle


def test_a_notice_is_appended_then_rung_with_no_guard_held(live):
    nid = _intent(live.root)
    outcome = _deliver(live, nid)
    assert (outcome.delivery, outcome.failure, outcome.settled) == ("wake-accepted", None, True)
    assert live.rings == [(MGR, ())]
    assert live.overlaps == []
    entry = _entry(live.root, nid)
    assert entry["delivery"] == "wake-accepted" and entry["claim"] is None
    assert entry["audit"][-1]["delivery"] == "wake-accepted"
    [item] = notice_delivery.pending(live.root, MGR)
    assert item["envelope"]["notice_id"] == nid
    assert _reservation(live.root)["state"] == "accepted"
    assert _reservation(live.root)["watermark"] == item["arrival"]


def test_failed_delivery_never_changes_the_dispatch_outcome(live):
    nid = _intent(live.root)
    before = load(live.root, DID)
    outcome = _deliver(live, nid, transport=lambda target, name: (False, "refused", "transport-refused"))
    after = load(live.root, DID)
    assert (outcome.delivery, outcome.failure) == ("wake-failed", "transport-refused")
    for key in ("state", "attempt", "chat_pending", "cost_usd", "verification_result", "notice_generation"):
        assert after.get(key) == before.get(key)
    assert _reservation(live.root)["state"] == "failed"
    assert nid in notice_delivery.eligible(after, clock=live.clock)


def test_a_current_claim_excludes_a_second_sender_until_it_expires(live):
    nid = _intent(live.root)
    first = notice_delivery.claim(live.root, DID, nid, clock=live.clock)
    assert isinstance(first, notice_delivery.Claim)
    live.clock.t += 10
    assert notice_delivery.claim(live.root, DID, nid, clock=live.clock) == "claimed"
    live.clock.t += notice_delivery.CLAIM_TTL_S
    second = notice_delivery.claim(live.root, DID, nid, clock=live.clock)
    assert isinstance(second, notice_delivery.Claim) and second.token != first.token

    late = notice_delivery.settle(live.root, first, "wake-accepted", None, clock=live.clock)
    assert late.settled is False and late.failure == "claim-expired"
    assert _entry(live.root, nid)["claim"]["token"] == second.token
    assert notice_delivery.settle(
        live.root, second, "wake-failed", "transport-timeout", clock=live.clock
    ).settled


def test_a_claim_stalled_past_its_ttl_stops_before_the_ring(live, monkeypatch):
    nid = _intent(live.root)
    real = notice_delivery.route_target

    def stalled(root, held):
        live.clock.t += notice_delivery.CLAIM_TTL_S + 1
        assert isinstance(notice_delivery.claim(root, DID, nid, clock=live.clock), notice_delivery.Claim)
        return real(root, held)

    monkeypatch.setattr(notice_delivery, "route_target", stalled)
    outcome = _deliver(live, nid)
    assert live.rings == []
    assert outcome.failure == "claim-expired" and outcome.settled is False
    assert _entry(live.root, nid)["delivery"] == "attempting"  # the competitor's claim stands


def test_a_crash_after_append_never_appends_again(live):
    nid = _intent(live.root)
    crashed = notice_delivery.claim(live.root, DID, nid, clock=live.clock)
    assert isinstance(crashed, notice_delivery.Claim)
    with peers.inbox_guard(live.root, MGR):
        assert notice_delivery.append(live.root, MGR, crashed.envelope, clock=live.clock)[0] == "appended"
    arrivals = peers.arrivals(live.root, MGR)
    live.clock.t += notice_delivery.CLAIM_TTL_S
    outcome = _deliver(live, nid)
    assert outcome.delivery == "wake-accepted"
    assert len(notice_delivery.pending(live.root, MGR)) == 1
    assert peers.arrivals(live.root, MGR) == arrivals
    assert len(live.rings) == 1


def test_a_crash_after_the_ring_coalesces_the_retry(live, monkeypatch):
    nid = _intent(live.root)
    crashed = [True]
    real = notice_delivery.settle
    monkeypatch.setattr(notice_delivery, "settle", lambda *a, **k: None if crashed[0] else real(*a, **k))
    _deliver(live, nid)  # the process dies after the ring, before settling the source
    crashed[0] = False
    assert _entry(live.root, nid)["delivery"] == "attempting"
    live.clock.t += notice_delivery.CLAIM_TTL_S
    outcome = _deliver(live, nid)
    assert (outcome.delivery, outcome.failure) == ("inbox-accepted", None)
    assert len(live.rings) == 1
    assert len(notice_delivery.pending(live.root, MGR)) == 1


def test_a_consumed_notice_is_never_queued_rung_or_retried_again(live):
    nid = _intent(live.root)
    save(live.root, {**RECORD, "id": "001-mac-02"})
    later = _intent(live.root, did="001-mac-02")
    with peers.inbox_guard(live.root, MGR):
        notice_delivery.tombstone_all(live.root, MGR, [nid], "hook-consumed", clock=live.clock)
    outcome = _deliver(live, nid)
    assert (outcome.delivery, outcome.failure) == ("inbox-accepted", "stale")
    assert notice_delivery.pending(live.root, MGR) == []
    assert live.rings == []
    assert _entry(live.root, nid)["consumption"] == "hook-consumed"

    for _ in range(3):  # recovery takes the oldest eligible id; a consumed one never fills the slot
        ready = sorted(
            (load(live.root, did)[notices.OUTBOX_FIELD][n]["recorded_at"], did, n)
            for did in (DID, "001-mac-02")
            for n in notice_delivery.eligible(load(live.root, did), clock=live.clock)
        )
        for _, did, n in ready[:1]:
            notice_delivery.deliver(live.root, did, n, clock=live.clock, transport=live.transport)
    assert notice_delivery.eligible(load(live.root, DID), clock=live.clock) == []
    assert [i["envelope"]["notice_id"] for i in notice_delivery.pending(live.root, MGR)] == [later]
    assert [sid for sid, _ in live.rings] == [MGR]


# ------------------------------------------------------------------ supersession and rechecks


def test_a_superseded_notice_is_not_sent(live):
    old = _intent(live.root, BLOCKED)
    new = _intent(live.root, DONE)
    outcome = _deliver(live, old)
    assert outcome.skipped == "stale" and outcome.delivery is None
    assert live.rings == []
    assert _entry(live.root, old)["delivery"] == "superseded"
    assert _entry(live.root, new)["delivery"] == "pending"


def test_supersession_after_the_append_stops_the_ring_and_fences_settlement(live, monkeypatch):
    """Atomicity spans neither source and inbox nor source and transport: the append stands
    and the receiving side must filter by the source's current disposition."""
    old = _intent(live.root, BLOCKED)
    real = notice_delivery.recheck
    calls = []

    def supersede_before_transport(root, held, *, clock):
        calls.append(1)
        if len(calls) == 2:  # the recheck after the append and before the ring
            _intent(live.root, DONE)
        return real(root, held, clock=clock)

    monkeypatch.setattr(notice_delivery, "recheck", supersede_before_transport)
    outcome = _deliver(live, old)
    assert live.rings == []
    assert outcome.failure == "stale" and outcome.settled is False
    assert _entry(live.root, old)["delivery"] == "superseded"
    assert [i["envelope"]["notice_id"] for i in notice_delivery.pending(live.root, MGR)] == [old]
    assert _reservation(live.root)["state"] == "failed"


# ------------------------------------------------------------------ bounded guards


def test_a_held_inbox_guard_records_a_fixed_fallback(live):
    nid = _intent(live.root)
    with _held_elsewhere(peers.inbox_guard(live.root, MGR)):
        outcome = _deliver(live, nid, timeout=0.2)
    assert (outcome.delivery, outcome.failure, outcome.settled) == ("fallback", "guard-timeout", True)
    assert _entry(live.root, nid)["failure"] == "guard-timeout"
    assert live.rings == []
    assert nid in notice_delivery.eligible(load(live.root, DID), clock=live.clock)


def test_a_held_session_lock_bounds_the_inbox_step_too(live):
    nid = _intent(live.root)
    with _held_elsewhere(sessions._locked(live.root, MGR)):
        began = time.monotonic()
        outcome = _deliver(live, nid, timeout=0.2)
        assert time.monotonic() - began < 2.0
    assert (outcome.delivery, outcome.failure, outcome.settled) == ("fallback", "guard-timeout", True)
    assert notice_delivery.pending(live.root, MGR) == []
    assert live.rings == []


def test_both_guards_take_an_optional_timeout(live):
    assert (notice_delivery.GUARD_TIMEOUT_S, notice_delivery.CLAIM_TTL_S) == (3.0, 30.0)
    with _held_elsewhere(peers.inbox_guard(live.root, MGR)):
        with pytest.raises(AgentError, match="busy"), peers.inbox_guard(live.root, MGR, timeout=0.1):
            pass
    with _held_elsewhere(record_guard(live.root, DID)):
        with pytest.raises(AgentError, match="busy"), record_guard(live.root, DID, timeout=0.1):
            pass


def test_a_held_record_guard_leaves_the_intent_recoverable(live):
    nid = _intent(live.root)
    with _held_elsewhere(record_guard(live.root, DID)):
        outcome = _deliver(live, nid, timeout=0.2)
    assert (outcome.delivery, outcome.failure, outcome.settled) == (None, "guard-timeout", False)
    entry = _entry(live.root, nid)
    assert entry["delivery"] == "pending" and entry["claim"] is None
    assert load(live.root, DID)["chat_pending"] is True


def test_an_unreadable_store_records_store_unavailable(live):
    nid = _intent(live.root)
    path = notice_delivery.store_path(live.root, MGR)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    outcome = _deliver(live, nid)
    assert (outcome.delivery, outcome.failure) == ("fallback", "store-unavailable")
    assert live.rings == []
    with pytest.raises(notices.NoticeError):
        notice_delivery.store_path(live.root, "../escape")


# ------------------------------------------------------------------ routes


def _assert_fallback(live, nid: str, failure: str) -> None:
    outcome = _deliver(live, nid)
    assert (outcome.delivery, outcome.failure) == ("fallback", failure)
    assert live.rings == []
    assert notice_delivery.pending(live.root, MGR) == []
    assert load(live.root, DID)["chat_pending"] is True


def test_a_missing_launcher_record_falls_back(live):
    _assert_fallback(live, _intent(live.root, session="mgr-gone"), "route-missing")


def test_an_ended_launcher_falls_back(live):
    sessions.set_field(live.root, MGR, "activity", {"state": "ended"})
    _assert_fallback(live, _intent(live.root), "route-ended")


def test_a_dead_claude_launcher_falls_back(live, monkeypatch):
    monkeypatch.setattr(notice_delivery, "is_alive", lambda pid: False)
    _assert_fallback(live, _intent(live.root), "route-ended")


def test_a_claude_launcher_without_an_inbox_key_falls_back(live, monkeypatch):
    monkeypatch.setattr(ops.peers, "inbox_token", lambda address: None)
    _assert_fallback(live, _intent(live.root), "auth-unavailable")


def test_a_worker_session_is_not_a_launcher(live):
    sessions.set_record(live.root, MGR, dispatch_id="001-mac-02")
    _assert_fallback(live, _intent(live.root), "route-unsupported")


def test_a_quiet_codex_launcher_stays_eligible_by_exact_id(live):
    sessions.set_field(live.root, MGR, "provider", "codex")
    sessions.set_field(live.root, MGR, "pid_unknown", True)
    sessions.set_field(live.root, MGR, "pid", None)
    outcome = _deliver(live, _intent(live.root))
    assert outcome.delivery == "wake-accepted"
    assert [sid for sid, _ in live.rings] == [MGR]


def test_an_intent_with_no_route_is_never_claimed(live):
    harness = notices.harness_identity(live.root)
    with record_guard(live.root, DID) as d:
        nid = notices.record_intent(d, DONE, harness=harness, launch_route=None)["notice_id"]
        save(live.root, d)
    outcome = _deliver(live, nid)
    assert outcome.skipped == "ineligible"
    assert live.rings == []


# ------------------------------------------------------------------ suppression


@pytest.mark.parametrize(
    "env", [{"AGENT_NOTIFY": "off"}, {"AGENT_NOTIFY": "no"}, {"AGENT_NOW": "2026-10-04T10:00:00-07:00"}]
)
def test_suppression_rings_nothing_and_keeps_the_fallback(live, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    nid = _intent(live.root)
    outcome = _deliver(live, nid)
    assert (outcome.delivery, outcome.failure) == ("inbox-accepted", "suppressed")
    assert live.rings == []
    assert _reservation(live.root) == {}
    assert load(live.root, DID)["chat_pending"] is True


def test_suppression_holds_at_the_transport_boundary_itself(live, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY", "off")
    target = sessions.get(live.root, MGR) or {}
    accepted, _, failure = ops.peers.ring(target, "mgr-one", purpose="dispatch")
    assert (accepted, failure) == (False, "suppressed")
    codex = {**target, "provider": "codex"}
    assert ops.peers.ring(codex, "mgr-one", purpose="dispatch")[2] == "suppressed"


def test_a_dispatch_without_a_chat_channel_is_never_delivered(live):
    d = load(live.root, DID)
    d["notify"] = ["macos"]
    save(live.root, d)
    nid = _intent(live.root)
    outcome = _deliver(live, nid)
    assert outcome.skipped == "suppressed"
    assert _entry(live.root, nid)["failure"] == "suppressed"
    assert notice_delivery.pending(live.root, MGR) == []
    assert live.rings == []


# ------------------------------------------------------------------ the shared wake reservation


def test_peer_then_dispatch_share_one_ring_and_one_acknowledgment(live):
    assert _peer_send(live.root, "hello") is not None
    nid = _intent(live.root, BLOCKED)
    outcome = _deliver(live, nid)
    assert (outcome.delivery, outcome.failure) == ("inbox-accepted", None)
    assert live.rings == []
    assert _reservation(live.root)["watermark"] == 2

    assert [r["text"] for r in peers.take(live.root, MGR, acknowledge_wake=True)] == ["hello"]
    assert _reservation(live.root) == {}
    later = _intent(live.root, DONE)
    assert _deliver(live, later).delivery == "wake-accepted"
    assert len(live.rings) == 1


def test_dispatch_then_peer_share_one_ring_and_one_acknowledgment(live):
    assert _deliver(live, _intent(live.root)).delivery == "wake-accepted"
    assert _peer_send(live.root, "hello") is None  # the dispatch ring covers it
    assert _reservation(live.root)["watermark"] == 2
    peers.take(live.root, MGR, acknowledge_wake=True)
    assert _reservation(live.root) == {}
    assert _peer_send(live.root, "again") is not None  # a later arrival rings again


def test_an_acknowledgment_keeps_arrivals_after_its_watermark(live):
    _peer_send(live.root, "first")
    with peers.inbox_guard(live.root, MGR):
        observed = peers.observe_wake(live.root, MGR)
    nid = _intent(live.root)
    _deliver(live, nid)  # arrives between observation and acknowledgment
    with peers.inbox_guard(live.root, MGR):
        later = peers.acknowledge_observed(live.root, MGR, observed)
    assert later is True
    assert [i["envelope"]["notice_id"] for i in notice_delivery.pending(live.root, MGR)] == [nid]
    assert notice_delivery.pending(live.root, MGR, upto=observed["watermark"]) == []
    assert peers.peek(live.root, MGR)[0]["text"] == "first"


def test_an_old_reservation_without_a_watermark_is_drained_and_cleared(live):
    old = {"id": "old", "state": "accepted", "at": now().timestamp()}
    sessions.set_field(live.root, MGR, peers.WAKE_FIELD, old)
    with peers.inbox_guard(live.root, MGR):
        peers.send(live.root, to=MGR, text="queued", frm="mgr-2")
        assert peer_wake.reserve(live.root, MGR) is None
    assert _reservation(live.root) == {**old, "watermark": 1}

    sessions.set_field(live.root, MGR, peers.WAKE_FIELD, old)
    with peers.inbox_guard(live.root, MGR):
        observed = peers.observe_wake(live.root, MGR)
        assert observed["id"] == "old"
        peers.acknowledge_observed(live.root, MGR, {"id": "other", "watermark": 1})
        assert _reservation(live.root) == old
    assert [r["text"] for r in peers.take(live.root, MGR, acknowledge_wake=True)] == ["queued"]
    assert _reservation(live.root) == {}
