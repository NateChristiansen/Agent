"""Dispatch notice envelope, identity and source outbox, the fixed wake text each purpose
rings with, and the shared reservation's watermark fence and bounded locks."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from agent_cli import ops, peers, sessions
from agent_cli.core import AgentError, now
from agent_cli.dispatch import machine, notice_delivery, notices
from agent_cli.dispatch.state import load, save
from agent_cli.dispatch.verification_gate import record_guard
from agent_cli.ops import peer_wake
from agent_cli.ops.peer_wake import DISPATCH_WAKE_TEXT
from notice_helpers import held_elsewhere as _held

DID = "001-mac-01"
RUN = "a" * 32
DONE = notices.Disposition(1, "terminal", "done", "verified", RUN, False, None)


def _record(root: Path, **extra) -> dict:
    d = {"id": DID, "short": "agent", "repo": "Agent", "feature": 23, "state": "done", "attempt": 1}
    d.update(extra)
    return save(root, d)


def _route(root: Path, session: str = "mgr-1") -> dict:
    return notices.route(notices.harness_identity(root), session)


def _intent(root: Path, disposition: notices.Disposition = DONE, **kwargs) -> dict:
    kwargs.setdefault("launch_route", _route(root))
    with record_guard(root, DID) as d:
        entry = notices.record_intent(d, disposition, harness=notices.harness_identity(root), **kwargs)
        save(root, d)
    return entry


def _envelope(root: Path) -> dict:
    _record(root)
    return dict(_intent(root)["envelope"])


def test_notice_id_is_sha256_of_the_canonical_array():
    nid = notices.notice_id("0123456789abcdef", DID, 2, "terminal", 3, None)
    raw = b'[1,"0123456789abcdef","001-mac-01",2,"terminal",3,null]'
    assert nid == hashlib.sha256(raw).hexdigest()
    assert nid == notices.notice_id("0123456789abcdef", DID, 2, "terminal", 3, None)
    assert nid != notices.notice_id("0123456789abcdef", DID, 2, "terminal", 3, RUN)


def test_harness_identity_reuses_the_persisted_install_token(tmp_root: Path):
    first = notices.harness_identity(tmp_root)
    assert first == (machine.read_install(tmp_root) or {})["token"]
    assert notices.harness_identity(tmp_root) == first


def test_a_replayed_disposition_keeps_its_id_timestamp_and_generation(tmp_root: Path):
    _record(tmp_root)
    first = _intent(tmp_root)
    d = load(tmp_root, DID)
    d["cost_usd"] = 1.25  # an ordinary revision does not advance the generation
    save(tmp_root, d)
    again = _intent(tmp_root)
    d = load(tmp_root, DID)
    assert again["notice_id"] == first["notice_id"]
    assert again["recorded_at"] == first["recorded_at"]
    assert d[notices.GENERATION_FIELD] == 1
    assert list(d[notices.OUTBOX_FIELD]) == [first["notice_id"]]
    assert d["chat_pending"] is True


def test_a_changed_disposition_supersedes_the_old_entry_and_keeps_its_audit(tmp_root: Path):
    _record(tmp_root)
    old = _intent(
        tmp_root,
        notices.Disposition(1, "attention", "running", "incomplete", None, True, "cleanup-incomplete"),
    )
    new = _intent(tmp_root)
    d = load(tmp_root, DID)
    assert new["notice_id"] != old["notice_id"] and new["generation"] == 2
    kept = d[notices.OUTBOX_FIELD][old["notice_id"]]
    assert kept["delivery"] == "superseded" and kept["superseded_by"] == new["notice_id"]
    assert kept["envelope"] == old["envelope"]
    assert notices.actionable(d, old["notice_id"]) is None
    assert notices.actionable(d, new["notice_id"]) is not None


def test_reverify_runs_and_attempts_get_distinct_ids(tmp_root: Path):
    _record(tmp_root)
    a = _intent(
        tmp_root, notices.Disposition(1, "verification-complete", "done", "verified", "b" * 32, False, None)
    )
    b = _intent(
        tmp_root, notices.Disposition(1, "verification-complete", "done", "verified", "c" * 32, False, None)
    )
    c = _intent(tmp_root, notices.Disposition(2, "terminal", "done", "verified", "c" * 32, False, None))
    assert len({a["notice_id"], b["notice_id"], c["notice_id"]}) == 3


def test_cancel_supersedes_outstanding_items_without_a_new_one(tmp_root: Path):
    _record(tmp_root)
    entry = _intent(tmp_root)
    with record_guard(tmp_root, DID) as d:
        assert notices.supersede(d) == [entry["notice_id"]]
        save(tmp_root, d)
    d = load(tmp_root, DID)
    assert len(d[notices.OUTBOX_FIELD]) == 1
    assert notices.actionable(d, entry["notice_id"]) is None


def test_a_missing_route_records_a_truthful_fallback(tmp_root: Path):
    _record(tmp_root)
    entry = _intent(tmp_root, launch_route=None)
    assert entry["envelope"] is None
    assert (entry["delivery"], entry["failure"]) == ("fallback", "route-missing")
    assert load(tmp_root, DID)["chat_pending"] is True


def test_an_intent_already_shown_to_its_caller_leaves_chat_pending_alone(tmp_root: Path):
    _record(tmp_root, chat_pending=False)
    entry = _intent(tmp_root, consumption="show-consumed")
    assert entry["consumption"] == "show-consumed"
    assert load(tmp_root, DID)["chat_pending"] is False


def test_routes_name_only_an_exact_local_session(tmp_root: Path):
    harness = notices.harness_identity(tmp_root)
    assert notices.route_problem(notices.route(harness, "mgr-1"), harness) is None
    assert notices.route_problem(None, harness) == "route-missing"
    remote = {**notices.route(harness, "mgr-1"), "locality": "remote"}
    assert notices.route_problem(remote, harness) == "route-unsupported"
    assert notices.route_problem(notices.route("f" * 16, "mgr-1"), harness) == "route-unsupported"
    assert notices.route_problem(notices.route(harness, "bad session"), harness) == "route-unsupported"


def test_the_envelope_has_exactly_the_version_1_fields(tmp_root: Path):
    envelope = _envelope(tmp_root)
    assert tuple(envelope) == notices.FIELDS
    assert envelope["kind"] == "dispatch.notice" and envelope["origin"] == "local-supervisor"
    assert envelope["recorded_at"].endswith("+00:00")
    assert notices.validate(envelope) == envelope
    assert len(notices.canonical(envelope)) <= notices.MAX_BYTES


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "peer.message"),
        ("origin", "worker"),
        ("version", True),
        ("attempt", 0),
        ("attempt", True),
        ("generation", -1),
        ("feature", 0),
        ("event_class", "progress"),
        ("state", "finished"),
        ("verification", "probably"),
        ("run_id", "not-a-run"),
        ("cleanup_pending", "no"),
        ("attention_reason", "question-pending"),
        ("recipient", "bad session id"),
        ("dispatch_id", "not-an-id"),
        ("dispatch_id", "023-mac-" + "1" * 130),
        ("recorded_at", "yesterday"),
        ("recorded_at", "2026-10-04T10:00:00-07:00"),
        ("notice_id", "0" * 64),
    ],
)
def test_the_validator_refuses_a_bad_field(tmp_root: Path, field: str, value):
    envelope = _envelope(tmp_root)
    envelope[field] = value
    with pytest.raises(notices.NoticeError):
        notices.validate(envelope)


def test_unknown_or_missing_fields_are_refused(tmp_root: Path):
    envelope = _envelope(tmp_root)
    with pytest.raises(notices.NoticeError):
        notices.validate({**envelope, "summary": "worker text"})
    missing = dict(envelope)
    del missing["state"]
    with pytest.raises(notices.NoticeError):
        notices.validate(missing)


def test_an_oversized_payload_is_refused_never_truncated(tmp_root: Path):
    envelope = _envelope(tmp_root)
    envelope["harness"] = "a" * 5000
    with pytest.raises(notices.NoticeError, match="4096 bytes"):
        notices.validate(envelope)
    assert envelope["harness"] == "a" * 5000


@pytest.mark.parametrize(
    "disposition",
    [
        notices.Disposition(1, "terminal", "running", "unverified", None, False, None),
        notices.Disposition(1, "attention", "question", "unverified", None, False, None),
        notices.Disposition(1, "terminal", "question", "unverified", None, False, "question-pending"),
        notices.Disposition(1, "verification-complete", "done", "verified", None, False, None),
    ],
)
def test_inconsistent_dispositions_are_refused(tmp_root: Path, disposition):
    _record(tmp_root)
    with pytest.raises(notices.NoticeError):
        _intent(tmp_root, disposition)
    assert notices.OUTBOX_FIELD not in load(tmp_root, DID)


def test_unknown_verification_evidence_reads_as_unverified():
    assert notices.assess_verification("verified") == "verified"
    assert notices.assess_verification("looks fine") == "unverified"
    assert notices.assess_verification(None) == "unverified"


def test_the_notice_carries_no_worker_text(tmp_root: Path):
    _record(
        tmp_root, result={"summary": "SECRET summary", "open_questions": ["SECRET question"]}, error="SECRET"
    )
    entry = _intent(
        tmp_root,
        notices.Disposition(1, "attention", "running", "unverified", None, False, "question-pending"),
    )
    assert "SECRET" not in json.dumps(entry)


# ------------------------------------------------------------------ wake purposes and fixed text

TARGET = {"session_id": "mgr-1", "provider": "claude", "inbox": "/nowhere/inbox.sock"}


@pytest.fixture
def rings_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.setenv("AGENT_NOTIFY", "on")
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["codex", *args])


def test_the_wake_helper_takes_only_the_two_purposes():
    assert ops.peers.wake_text("peer") == ops.peers.WAKE_TEXT
    assert ops.peers.wake_text("dispatch") == DISPATCH_WAKE_TEXT
    with pytest.raises(ValueError):
        ops.peers.wake_text("anything else")  # type: ignore[arg-type]


def test_the_dispatch_text_asks_for_no_peer_reply():
    text = DISPATCH_WAKE_TEXT
    assert text.startswith("Dispatch supervisor notice in this turn's context.")
    assert "No peer reply" in text
    assert "action list" in text
    assert text != ops.peers.WAKE_TEXT


def test_each_purpose_rings_with_its_own_fixed_text(rings_on, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(ops.peers, "_ring_socket", lambda *args: sent.append(args))
    assert ops.peers.ring(TARGET, "mgr-one", purpose="dispatch")[0] is True
    assert ops.peers.wake_peer(TARGET, "mgr-one")[0] is True
    assert sent == [("/nowhere/inbox.sock", "dispatch"), ("/nowhere/inbox.sock",)]

    argv: list[list[str]] = []
    monkeypatch.setattr(ops.peers, "_run", lambda args, env=None: argv.append(list(args)) or True)
    codex = {**TARGET, "provider": "codex"}
    assert ops.peers.ring(codex, "mgr-one", purpose="dispatch")[0] is True
    assert ops.peers.wake_peer(codex, "mgr-one")[0] is True
    assert [a[-1] for a in argv] == [DISPATCH_WAKE_TEXT, ops.peers.WAKE_TEXT]


def test_a_dispatch_frame_carries_the_dispatch_text(monkeypatch):
    frames: list[bytes] = []
    monkeypatch.setattr(ops.peers, "inbox_token", lambda address: None)

    class Socket:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def settimeout(self, value):
            pass

        def connect(self, address):
            pass

        def sendall(self, payload):
            frames.append(payload)

    monkeypatch.setattr("socket.socket", Socket)
    monkeypatch.setattr("socket.AF_UNIX", 1, raising=False)
    assert ops.peers._ring_socket("/nowhere/inbox.sock", "dispatch") is None
    assert ops.peers._ring_socket("/nowhere/inbox.sock") is None
    contents = [json.loads(f)["message"]["content"] for f in frames]
    assert contents == [DISPATCH_WAKE_TEXT, ops.peers.WAKE_TEXT]


def test_ring_failures_map_to_fixed_classes(rings_on, monkeypatch):
    monkeypatch.setattr(ops.peers, "_ring_socket", lambda *args: "TimeoutError: timed out")
    assert ops.peers.ring(TARGET, "m", purpose="dispatch")[2] == "transport-timeout"
    monkeypatch.setattr(ops.peers, "_ring_socket", lambda *args: "ConnectionRefusedError: [Errno 61]")
    assert ops.peers.ring(TARGET, "m", purpose="dispatch")[2] == "transport-refused"
    monkeypatch.setattr(ops.peers, "_run", lambda args, env=None: False)
    assert ops.peers.ring({**TARGET, "provider": "codex"}, "m", purpose="dispatch")[2] == "queue-failed"
    assert ops.peers.ring({"session_id": "x"}, "m", purpose="dispatch")[2] == "route-missing"
    failures = {"transport-timeout", "transport-refused", "queue-failed", "route-missing", "suppressed"}
    assert failures <= set(notice_delivery.FAILURES)


# ------------------------------------------------------------------ the reservation's fence and bounds

MGR = "mgr-1"


def _send(root: Path, text: str) -> str | None:
    with peers.inbox_guard(root, MGR):
        peers.send(root, to=MGR, text=text, frm="mgr-2")
        return peer_wake.reserve(root, MGR)


def _reservation(root: Path) -> dict:
    return (sessions.get(root, MGR) or {}).get(peers.WAKE_FIELD) or {}


def test_an_ack_keeps_the_same_reservation_once_a_later_append_extended_it(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    wake_id = _send(tmp_root, "first")
    assert wake_id is not None
    peer_wake.settle(tmp_root, MGR, wake_id, True)
    with peers.inbox_guard(tmp_root, MGR):
        observed = peers.observe_wake(tmp_root, MGR)
    assert observed == {"id": wake_id, "watermark": 1}
    assert _send(tmp_root, "later") is None  # coalesced into the same reservation, no ring
    assert _reservation(tmp_root) == {**_reservation(tmp_root), "id": wake_id, "watermark": 2}

    with peers.inbox_guard(tmp_root, MGR):
        assert peers.acknowledge_observed(tmp_root, MGR, observed) is True
    assert _reservation(tmp_root)["id"] == wake_id  # still covers the arrival the reader missed

    # The reader that sees the later arrival acknowledges it; the next arrival rings again.
    assert [r["text"] for r in peers.take(tmp_root, MGR, acknowledge_wake=True)] == ["first", "later"]
    assert _reservation(tmp_root) == {}
    assert _send(tmp_root, "after") is not None


def test_an_acknowledgment_never_clears_a_newer_reservation(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    _send(tmp_root, "first")
    with peers.inbox_guard(tmp_root, MGR):
        observed = peers.observe_wake(tmp_root, MGR)
    newer = {"id": "newer", "state": "attempting", "at": 1.0, "watermark": 1}
    sessions.set_field(tmp_root, MGR, peers.WAKE_FIELD, newer)
    with peers.inbox_guard(tmp_root, MGR):
        peers.acknowledge_observed(tmp_root, MGR, observed)
    assert _reservation(tmp_root) == newer


def test_a_send_to_a_session_with_no_record_creates_none(tmp_root: Path):
    peers.send(tmp_root, to="nobody-here", text="hello", frm="mgr-2")
    assert sessions.get(tmp_root, "nobody-here") is None
    assert peers.arrivals(tmp_root, "nobody-here") == 0
    assert [r["text"] for r in peers.peek(tmp_root, "nobody-here")] == ["hello"]


def test_the_session_lock_bounds_counting_an_arrival(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    with _held(sessions._locked(tmp_root, MGR)):
        began = time.monotonic()
        with peers.inbox_guard(tmp_root, MGR, timeout=0.05):
            with pytest.raises(AgentError, match="is busy"):
                peers.note_arrival(tmp_root, MGR, timeout=0.05)
        assert time.monotonic() - began < 0.3
    assert peers.arrivals(tmp_root, MGR) == 0


def test_the_session_lock_bounds_reserving_and_acknowledging(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    wake_id = _send(tmp_root, "first")
    failed = {**_reservation(tmp_root), "state": "failed"}  # not current, so each call must write
    sessions.set_field(tmp_root, MGR, peers.WAKE_FIELD, failed)
    with _held(sessions._locked(tmp_root, MGR)):
        began = time.monotonic()
        assert [r["text"] for r in peers.take(tmp_root, MGR, acknowledge_wake=True, timeout=0.05)] == [
            "first"
        ]
        with pytest.raises(AgentError, match="is busy"):
            peer_wake.settle(tmp_root, MGR, str(wake_id), True, timeout=0.05)
        with peers.inbox_guard(tmp_root, MGR), pytest.raises(AgentError, match="is busy"):
            peer_wake.reserve(tmp_root, MGR, timeout=0.05)
        assert time.monotonic() - began < 1.0
    assert _reservation(tmp_root) == failed


def _rows(rows: list[dict]) -> list[str]:
    return [r["text"] for r in rows]


def test_a_failed_acknowledgment_keeps_the_rows_and_the_next_send_rings(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    wake_id = str(_send(tmp_root, "first"))
    peer_wake.settle(tmp_root, MGR, wake_id, True)
    with _held(sessions._locked(tmp_root, MGR)):
        assert _rows(peers.take(tmp_root, MGR, acknowledge_wake=True, timeout=0.05)) == ["first"]
    assert peers.peek(tmp_root, MGR) == [] and _reservation(tmp_root)["id"] == wake_id
    assert _send(tmp_root, "second") not in (None, wake_id)  # rings instead of coalescing
    assert _send(tmp_root, "third") is None  # and only once
    assert _rows(peers.take(tmp_root, MGR, acknowledge_wake=True)) == ["second", "third"]
    assert _reservation(tmp_root) == {} and not peers.stale_path(tmp_root, MGR).exists()


def test_once_the_lock_frees_the_stale_reservation_clears_and_rings_once(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    peer_wake.settle(tmp_root, MGR, str(_send(tmp_root, "first")), True)
    with _held(sessions._locked(tmp_root, MGR)):
        peers.take(tmp_root, MGR, acknowledge_wake=True, timeout=0.05)
    assert peers.take(tmp_root, MGR, acknowledge_wake=True) == []
    assert _reservation(tmp_root) == {} and not peers.stale_path(tmp_root, MGR).exists()
    assert _send(tmp_root, "next") is not None
    assert _send(tmp_root, "again") is None


def test_a_failed_marker_write_drains_nothing(tmp_root: Path, monkeypatch):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    wake_id = _send(tmp_root, "first")
    real = peers.atomic_write

    def refuse_marker(path: Path, text: str) -> None:
        if path.name.endswith(".stale.json"):
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(peers, "atomic_write", refuse_marker)
    with pytest.raises(AgentError):
        peers.take(tmp_root, MGR, acknowledge_wake=True, timeout=0.05)
    assert _rows(peers.peek(tmp_root, MGR)) == ["first"] and _reservation(tmp_root)["id"] == wake_id


def test_a_stale_marker_names_one_reservation_at_one_watermark(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    sessions.set_counter(tmp_root, MGR, peers.ARRIVALS_FIELD, 2)
    peers.mark_stale(tmp_root, MGR, {"id": "r1", "watermark": 1})
    at = now().timestamp()
    for reservation, rings in (({"id": "r2", "watermark": 1}, False), ({"id": "r1", "watermark": 2}, False)):
        sessions.set_field(tmp_root, MGR, peers.WAKE_FIELD, {**reservation, "state": "accepted", "at": at})
        with peers.inbox_guard(tmp_root, MGR):
            assert (peer_wake.reserve(tmp_root, MGR) is not None) is rings
    sessions.set_field(
        tmp_root, MGR, peers.WAKE_FIELD, {"id": "r1", "state": "accepted", "at": at, "watermark": 1}
    )
    peer_wake.settle(tmp_root, MGR, "r1", True)
    with peers.inbox_guard(tmp_root, MGR):
        assert peer_wake.reserve(tmp_root, MGR) is not None
    assert peers.stale_path(tmp_root, MGR).exists()  # reserve and settle never delete it


def test_the_session_lock_still_waits_without_a_timeout(tmp_root: Path):
    sessions.set_record(tmp_root, MGR, name="mgr-one")
    done = threading.Event()

    def write():
        sessions.set_field(tmp_root, MGR, "probe", 1)
        done.set()

    with _held(sessions._locked(tmp_root, MGR)):
        writer = threading.Thread(target=write)
        writer.start()
        assert not done.wait(0.2)
    writer.join(5)
    assert (sessions.get(tmp_root, MGR) or {})["probe"] == 1
