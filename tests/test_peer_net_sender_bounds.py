"""One deadline for every step of a send, no leaked snapshots, honest summaries (
task 5's second code review of 2026-10-04)."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time

import pytest

from agent_cli.ops.peer_remote import summary
from agent_cli.peer_net import mutate, outbox, sender, ssh, store
from agent_cli.peer_net.channel import Clock
from agent_cli.peer_net.errors import PeerNetError
from peer_net_route_fixture import paired  # noqa: F401 -- the fixture

# Timed from when the guard hold starts. A connect that spent a fresh deadline on each step waits
# for the guard (released at least the hold after that) and then a whole deadline on the held
# write lock, so it ends at least their sum later, however late the test thread starts it. One
# shared deadline ends near the deadline after connect starts; the guard hold is its headroom on
# every platform, for that start delay and any overrun together.
CONNECT_DEADLINE_S = 1.0
GUARD_HOLD_S = 0.5


def _send(root, **kw):
    return sender.send(
        root, alias="mac", to="mgr-b", text="hello", from_session="cl-sender-a", from_name="", **kw
    )


def test_store_connect_spends_one_deadline_across_guard_and_setup(paired):
    devices, *_ = paired
    from agent_cli.dispatch.guards import area_guard

    db = devices.a / store.STORE_REL
    raw = sqlite3.connect(db, isolation_level=None)
    raw.execute("DROP TABLE outbox")  # connect must recreate it, which needs the write lock
    raw.execute("BEGIN IMMEDIATE")  # another writer holds that lock throughout
    held, release = threading.Event(), threading.Event()
    hold_started: list[float] = []

    def hold_guard() -> None:
        with area_guard(devices.a, "peer-net:store-init"):
            hold_started.append(time.monotonic())
            held.set()
            release.wait(GUARD_HOLD_S)

    holder = threading.Thread(target=hold_guard)
    holder.start()
    assert held.wait(5)
    started = hold_started[0]
    try:
        with pytest.raises(PeerNetError) as caught:
            store.connect(devices.a, timeout=CONNECT_DEADLINE_S)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(5)
        raw.execute("ROLLBACK")
        raw.close()
    assert caught.value.code == "timeout" and elapsed < CONNECT_DEADLINE_S + GUARD_HOLD_S


@pytest.mark.parametrize(
    ("text", "code"),
    [("x" + "\x01" * 8191, "oversized"), ("\ud800", "bad-value")],
    ids=["escapes past the envelope cap", "lone surrogate"],
)
def test_an_envelope_the_receiver_would_refuse_is_refused_before_the_journal(paired, text, code):
    devices, wire, *_ = paired
    with pytest.raises(PeerNetError) as caught:
        sender.send(devices.a, alias="mac", to="mgr-b", text=text, from_session="cl-sender-a", from_name="")
    assert caught.value.code == code and wire.calls == []
    conn = store.connect(devices.a)
    try:
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_snapshot_whose_write_fails_leaves_nothing_behind(paired, monkeypatch):
    devices, *_ = paired
    conn = store.connect(devices.a)
    try:
        route = sender._route(conn, "mac")
    finally:
        conn.close()
    assert route is not None

    def half_written(path, data):
        path.write_bytes(data[:3])
        raise OSError("disk full")

    monkeypatch.setattr(mutate, "write_private", half_written)
    with pytest.raises(PeerNetError) as caught:
        sender.pin_snapshot(devices.a, route)
    assert caught.value.code == "snapshot"
    directory = devices.a / sender.SNAPSHOTS_REL
    assert not directory.exists() or list(directory.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege the Windows test user lacks")
def test_a_linked_snapshot_directory_is_refused(paired, tmp_path):
    devices, *_ = paired
    conn = store.connect(devices.a)
    try:
        route = sender._route(conn, "mac")
    finally:
        conn.close()
    assert route is not None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (devices.a / sender.SNAPSHOTS_REL).symlink_to(elsewhere)
    with pytest.raises(PeerNetError):
        sender.pin_snapshot(devices.a, route)
    assert list(elsewhere.iterdir()) == []


def test_a_failure_after_the_journal_still_returns_the_id(paired, monkeypatch):
    devices, *_ = paired

    def broken(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(outbox, "open_for", broken)
    result = _send(devices.a)
    assert len(result["id"]) == 32 and result["outcome"] == "accepted" and result["backlog"] is None


def test_a_same_state_lost_settle_does_not_claim_the_ack(paired, monkeypatch):
    devices, *_ = paired
    real = outbox.settle

    def contested(conn, msg_id, rid, state, outcome, **kw):
        with store.transaction(conn):  # another attempt settled the very same state first
            conn.execute("UPDATE outbox SET state=?, claim_rid='other' WHERE id=?", (state, msg_id))
        return real(conn, msg_id, rid, state, outcome, **kw)

    monkeypatch.setattr(outbox, "settle", contested)
    result = _send(devices.a)
    assert result["outcome"] == "accepted" and result["answer"] is None
    assert "recorded by another attempt" in summary(result, "mac")


def test_the_backlog_counts_only_earlier_messages(paired):
    devices, wire, *_ = paired
    wire.faults = ["lost", "lost", "lost"]
    result = _send(devices.a)
    assert result["outcome"] == "unconfirmed" and result["backlog"] == 0


@pytest.mark.parametrize(
    ("outcome", "must", "must_not"),
    [
        ("acceptance-unknown", "not resent", "resent with the next"),
        ("abandoned:route-changed", "no further attempt", "resent"),
        ("abandoned:no-route", "no further attempt", "resent"),
        ("skipped", "another attempt", "accepted on"),
        ("uncertain", "could not be confirmed", "accepted on"),
        ("refused:disabled", "may still have been accepted", "retried with"),
        ("unconfirmed", "resent with the next send", "accepted on"),
    ],
)
def test_every_outcome_has_a_truthful_summary(outcome, must, must_not):
    text = summary({"id": "c" * 32, "outcome": outcome, "answer": None, "earlier": [], "backlog": 0}, "mac")
    assert must in text and must_not not in text
    # No outcome proves a message never arrived: an earlier attempt's ACK may have been lost.
    assert "not delivered" not in text.lower() and "was not sent" not in text


def test_a_refusal_after_a_lost_ack_does_not_hide_the_earlier_acceptance(paired, monkeypatch):
    devices, wire, a_sid, b_sid = paired
    from agent_cli.peer_net import enroll
    from peer_net_pair_fixture import as_b

    real_run = wire.run

    def drop_then_disable(command, stdin, **kw):
        status, out, overflow = real_run(command, stdin, **kw)
        if len(wire.calls) == 1:  # the first attempt was accepted; its answer is lost
            as_b(lambda: enroll.set_enabled(devices.b, False))
            return None, b"", False
        return status, out, overflow

    monkeypatch.setattr(ssh, "run_bounded", drop_then_disable)
    result = _send(devices.a)
    assert result["outcome"] == "refused:disabled"  # the receiver refuses before any history
    from agent_cli import peers as peers_mod

    assert len(peers_mod.peek(devices.b, b_sid)) == 1  # yet the first attempt was accepted
    assert "may still have been accepted" in summary(result, "mac")


def test_an_unknown_backlog_is_said_to_be_unknown():
    text = summary(
        {"id": "c" * 32, "outcome": "unconfirmed", "answer": None, "earlier": [], "backlog": None}, "mac"
    )
    assert "unknown" in text


def test_run_bounded_returns_within_its_timeout_cleanup_included():
    started = time.monotonic()
    status, _, _ = ssh.run_bounded(
        [sys.executable, "-c", "import time; time.sleep(60)"], b"", timeout=1.0, limit=64
    )
    assert status is None and time.monotonic() - started < 1.0 + 0.3


def test_slow_windows_permission_checks_stay_inside_the_send_budget(paired, monkeypatch):
    devices, wire, *_ = paired
    import subprocess as subprocess_mod

    from agent_cli.peer_net import system

    def slow_powershell(command, **kwargs):
        time.sleep(min(kwargs["timeout"], 10))  # a PowerShell start that never finishes in time
        raise subprocess_mod.TimeoutExpired("powershell", kwargs["timeout"])

    monkeypatch.setattr(system.subprocess, "run", slow_powershell)
    # As on Windows: the store's privacy check goes through PowerShell.
    monkeypatch.setattr(store, "check_private", lambda root: system.acl_facts(root / store.STORE_REL))
    started = time.monotonic()
    with pytest.raises(PeerNetError):
        _send(devices.a, clock=Clock(1.5))
    assert time.monotonic() - started < 2.0 and wire.calls == []


def test_the_send_budget_is_one_clock_from_the_tool_path(paired, monkeypatch):
    devices, wire, *_ = paired
    wire.faults = ["hang"] * 5
    started = time.monotonic()
    result = _send(devices.a, clock=Clock(1.5))
    assert time.monotonic() - started < 2.0 and result["outcome"] in {"unconfirmed", "uncertain"}
