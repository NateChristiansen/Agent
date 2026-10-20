"""Sending across devices: names, exact ACKs, the outbox, replay rules and the send budget."""

from __future__ import annotations

import json
import sys
import time

import pytest

from agent_cli import peers as peers_mod
from agent_cli.ops.peer_remote import target
from agent_cli.peer_net import ack, outbox, sender, ssh, store
from agent_cli.peer_net.errors import PeerNetError
from peer_net_route_fixture import paired  # noqa: F401 -- the fixture


def _send(root, who="mgr-b", text="hello mac", sid="cl-sender-a", **kw):
    return sender.send(root, alias="mac", to=who, text=text, from_session=sid, from_name="mgr-a", **kw)


def _rows(root, sid):
    return peers_mod.peek(root, sid)


def _outbox(root):
    conn = store.connect(root)
    try:
        return conn.execute("SELECT id, state, attempts FROM outbox ORDER BY created_at").fetchall()
    finally:
        conn.close()


# --- names --------------------------------------------------------------------------------


def test_names_split_into_device_and_session(paired):
    devices, *_ = paired
    assert sender.split("mgr-a") == (None, "mgr-a")
    assert sender.split("mac/cx 1234") == ("mac", "cx 1234")
    assert target(devices.a, "mac/mgr-b") == ("mac", "mgr-b")
    assert target(devices.a, "win/mgr-a") == (None, "mgr-a")  # this device's own alias is local
    for bad in ["Mac/x", "mac/", "mac/  ", "../x"]:
        with pytest.raises(PeerNetError):
            sender.split(bad)


def test_an_unrouted_device_is_refused_before_any_io(paired):
    devices, wire, *_ = paired
    with pytest.raises(PeerNetError) as caught:
        sender.send(devices.a, alias="linux", to="x", text="hi", from_session="cl-sender-a", from_name="")
    assert caught.value.code == "unknown-device" and "mac" in caught.value.detail
    assert wire.calls == [] and _outbox(devices.a) == []


def test_envelope_bounds_are_checked_locally(paired):
    devices, wire, *_ = paired
    for text in ["", "   ", "x" * 9000]:
        with pytest.raises(PeerNetError):
            _send(devices.a, text=text)
    assert wire.calls == [] and _outbox(devices.a) == []


# --- delivery and replies ---------------------------------------------------------------------


def test_a_message_is_accepted_and_a_reply_returns_to_the_origin(paired):
    devices, wire, a_sid, b_sid = paired
    result = _send(devices.a)
    assert result["outcome"] == "accepted" and result["answer"]["to_name"] == "mgr-b"
    (row,) = _rows(devices.b, b_sid)
    assert row["from"] == f"win/{a_sid}" and row["text"] == "hello mac"
    reply = sender.send(
        devices.b, alias="win", to=a_sid, text="hi back", from_session=b_sid, from_name="mgr-b"
    )
    assert reply["outcome"] == "accepted"
    (back,) = _rows(devices.a, a_sid)
    assert back["from"] == f"mac/{b_sid}" and back["text"] == "hi back"


def test_an_invalid_from_name_is_sent_empty(paired):
    devices, wire, a_sid, b_sid = paired
    sender.send(devices.a, alias="mac", to="mgr-b", text="hi", from_session=a_sid, from_name="bad\nname")
    assert _rows(devices.b, b_sid)[0]["from_name"] == f"win/{a_sid}"


def test_a_refusal_is_final_and_never_retried(paired):
    devices, wire, *_ = paired
    result = _send(devices.a, who="nobody")
    assert result["outcome"] == "refused:unknown-recipient"
    assert len(wire.calls) == 1 and _outbox(devices.a)[0][1:] == ("refused:unknown-recipient", 1)


def test_a_lost_ack_is_retried_with_the_same_bytes_and_delivers_once(paired):
    devices, wire, a_sid, b_sid = paired
    wire.faults = ["drop"]  # delivered, but the answer is lost
    result = _send(devices.a)
    assert result["outcome"] == "accepted" and result["answer"]["state"] == "already-accepted"
    assert len(_rows(devices.b, b_sid)) == 1 and len(wire.calls) == 2


def test_a_malformed_ack_is_no_answer_never_accepted(paired):
    devices, wire, a_sid, b_sid = paired
    wire.faults = ["garbage", "garbage", "garbage"]
    result = _send(devices.a)
    assert result["outcome"] == "unconfirmed" and result["answer"] is None
    assert len(_rows(devices.b, b_sid)) == 1  # it was delivered; the sender just cannot know
    assert _outbox(devices.a)[0][1:] == ("unconfirmed", 3)


def test_an_unconfirmed_send_is_replayed_after_the_next_new_message(paired):
    devices, wire, a_sid, b_sid = paired
    wire.faults = ["lost", "lost", "lost"]
    first = _send(devices.a, text="first")
    assert first["outcome"] == "unconfirmed" and first["backlog"] == 0  # no earlier ones
    second = _send(devices.a, text="second")
    assert second["outcome"] == "accepted"
    assert second["earlier"] == [{"id": first["id"], "outcome": "accepted"}] and second["backlog"] == 0
    assert [r["text"] for r in _rows(devices.b, b_sid)] == ["second", "first"]  # new first
    assert [c["fault"] for c in wire.calls][-2:] == ["ok", "ok"]


def test_a_pending_row_left_by_a_crash_is_replayed(paired, monkeypatch):
    devices, wire, a_sid, b_sid = paired
    real = sender._attempt

    def crash_before_first_attempt(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(sender, "_attempt", crash_before_first_attempt)
    with pytest.raises(RuntimeError):
        _send(devices.a, text="journaled only")
    assert _outbox(devices.a)[0][1] == "pending"
    monkeypatch.setattr(sender, "_attempt", real)
    result = _send(devices.a, text="next")
    assert result["earlier"][0]["outcome"] == "accepted"
    assert sorted(r["text"] for r in _rows(devices.b, b_sid)) == ["journaled only", "next"]


def test_a_claim_is_exclusive_and_a_stale_attempt_never_overwrites(paired):
    devices, *_ = paired
    conn = store.connect(devices.a)
    try:
        outbox.journal(
            conn,
            msg_id="a" * 32,
            alias="mac",
            route={},
            envelope=b"{}",
            expires_at=time.time() + 600,
            now=time.time(),
        )
        old = outbox.claim(conn, "a" * 32, lease_s=0.0, now=time.time())
        assert old and outbox.claim(conn, "a" * 32, lease_s=30, now=time.time() - 1) is None  # lease live
        new = outbox.claim(conn, "a" * 32, lease_s=30, now=time.time() + 1)  # the old lease lapsed
        assert new and new != old
        assert outbox.settle(conn, "a" * 32, old, "accepted", "stale", now=time.time()) is False
        assert outbox.settle(conn, "a" * 32, new, "refused:x", "fresh", now=time.time()) is True
        assert outbox.claim(conn, "a" * 32, lease_s=30, now=time.time() + 99) is None  # closed for good
    finally:
        conn.close()


def _open_row(root, *, expires_in: float, route=None):
    msg_id, raw, _ = sender._envelope(to="mgr-b", text="old", from_session="cl-sender-a", from_name="")
    conn = store.connect(root)
    try:
        frozen = route if route is not None else sender._route(conn, "mac")
        assert frozen is not None
        at = sender._now()
        outbox.journal(
            conn, msg_id=msg_id, alias="mac", route=frozen, envelope=raw, expires_at=at + expires_in, now=at
        )
    finally:
        conn.close()
    return msg_id


def test_an_expired_row_closes_as_acceptance_unknown_with_no_network(paired):
    devices, wire, *_ = paired
    msg_id = _open_row(devices.a, expires_in=-1)
    result = _send(devices.a)
    assert {"id": msg_id, "outcome": "acceptance-unknown"} in result["earlier"]
    assert len(wire.calls) == 1  # only the new message went out


def test_a_replay_at_expiry_reaches_a_receiver_whose_clock_is_behind(paired):
    devices, wire, a_sid, b_sid = paired
    msg_id = _open_row(devices.a, expires_in=0)  # the sender's now is exactly E: still sendable
    wire.receiver_skew_s = -300.0
    result = _send(devices.a)
    assert {"id": msg_id, "outcome": "accepted"} in result["earlier"]
    assert sorted(r["text"] for r in _rows(devices.b, b_sid)) == ["hello mac", "old"]


def test_a_changed_destination_abandons_the_saved_message(paired):
    devices, wire, *_ = paired
    conn = store.connect(devices.a)
    try:
        current = sender._route(conn, "mac")
        assert current is not None
        frozen = {**current, "user": "someone-else"}  # same host key, another account
    finally:
        conn.close()
    msg_id = _open_row(devices.a, expires_in=600, route=frozen)
    result = _send(devices.a)
    assert {"id": msg_id, "outcome": "abandoned:route-changed"} in result["earlier"]
    assert len(wire.calls) == 1


def test_a_revoked_route_abandons_the_saved_message_without_sending(paired):
    devices, wire, *_ = paired
    msg_id = _open_row(devices.a, expires_in=600)
    conn = store.connect(devices.a)
    try:
        with store.transaction(conn):
            conn.execute("DELETE FROM outbound WHERE alias='mac'")
        item = outbox.get(conn, msg_id)
        assert item is not None
        assert sender._replay(devices.a, conn, "win", item, sender.Clock(10)) == "abandoned:no-route"
    finally:
        conn.close()
    assert wire.calls == []


def test_the_whole_send_is_bounded_by_its_budget(paired):
    devices, wire, *_ = paired
    _open_row(devices.a, expires_in=600)
    wire.faults = ["hang"] * 10
    started = time.monotonic()
    result = _send(devices.a, clock=sender.Clock(2.0))
    assert time.monotonic() - started < 3.0
    assert result["outcome"] == "unconfirmed"
    assert result["backlog"] is None  # the budget ran out: the backlog is unknown, never guessed
    assert len(wire.calls) == 1  # no second attempt or replay started without budget


def test_the_replay_batch_is_capped(paired):
    devices, wire, *_ = paired
    for _ in range(5):
        _open_row(devices.a, expires_in=600)
    result = _send(devices.a)
    assert len(result["earlier"]) == sender.REPLAY_BATCH and result["backlog"] == 5 - sender.REPLAY_BATCH


# --- the ACK shape ------------------------------------------------------------------------------

GOOD = {
    "v": 1,
    "ok": True,
    "state": "accepted",
    "queue": "queued",
    "wake": "accepted",
    "recovery": "none",
    "id": "b" * 32,
    "to_name": "mgr-b",
}


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


@pytest.mark.parametrize(
    ("status", "out"),
    [
        (0, b"{}\n"),
        (0, _line({**GOOD, "ok": "true"})),
        (0, _line({**GOOD, "id": "c" * 32})),
        (0, _line({**GOOD, "extra": 1})),
        (0, _line({**GOOD, "state": "read"})),
        (0, b'{"v": 1, "v": 1, "ok": true}\n'),
        (0, _line(GOOD) + _line(GOOD)),
        (0, _line({**GOOD, "to_name": "x" * 129})),
        (2, _line(GOOD)),
        (1, _line(GOOD)),
        (0, _line({"v": 1, "ok": False, "error": "BAD CODE", "detail": "x"})),
        (1, _line({"v": 1, "ok": False, "error": "conflict", "detail": "x" * 65})),
        (None, b""),
        (0, b"x" * (ack.ACK_LIMIT + 1)),
    ],
)
def test_anything_but_the_exact_ack_is_no_answer(status, out):
    assert ack.enqueue_answer(status, out, "b" * 32) is None


def test_the_exact_ack_and_refusal_are_answers():
    assert ack.enqueue_answer(0, _line(GOOD), "b" * 32) == GOOD
    refusal = {"v": 1, "ok": False, "error": "disabled", "detail": "receiver"}
    assert ack.enqueue_answer(1, _line(refusal), "b" * 32) == refusal


# --- the bounded ssh call ---------------------------------------------------------------------


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_run_bounded_kills_on_overflow_while_capturing():
    started = time.monotonic()
    status, out, overflow = ssh.run_bounded(
        _py("import sys; sys.stdout.write('x' * 10_000_000)"), b"", timeout=20, limit=4096
    )
    assert overflow and status is None and len(out) == 4096 and time.monotonic() - started < 10


def test_run_bounded_ends_a_hanging_process_at_its_timeout():
    started = time.monotonic()
    status, out, overflow = ssh.run_bounded(_py("import time; time.sleep(60)"), b"", timeout=0.5, limit=4096)
    assert status is None and not overflow and time.monotonic() - started < 5


def test_run_bounded_returns_the_status_and_output():
    status, out, overflow = ssh.run_bounded(
        _py("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper() + b'\\n'); sys.exit(1)"),
        b"hi",
        timeout=10,
        limit=4096,
    )
    assert (status, out, overflow) == (1, b"HI\n", False)
