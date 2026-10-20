"""The sender re-checks everything after its claim and reports only durable state (
task 5's code review of 2026-10-04)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import timedelta

import pytest

from agent_cli.core import now
from agent_cli.peer_net import ack, outbox, sender, ssh, store
from agent_cli.peer_net.channel import Clock
from agent_cli.peer_net.errors import PeerNetError
from peer_net_pair_fixture import host_line
from peer_net_route_fixture import paired  # noqa: F401 -- the fixture


def _send(root, text="hello mac", **kw):
    return sender.send(
        root, alias="mac", to="mgr-b", text=text, from_session="cl-sender-a", from_name="mgr-a", **kw
    )


def _open_row(root, *, expires_in: float = 600.0) -> str:
    msg_id, raw, _ = sender._envelope(to="mgr-b", text="old", from_session="cl-sender-a", from_name="")
    conn = store.connect(root)
    try:
        route = sender._route(conn, "mac")
        assert route is not None
        at = sender._now()
        outbox.journal(
            conn, msg_id=msg_id, alias="mac", route=route, envelope=raw, expires_at=at + expires_in, now=at
        )
    finally:
        conn.close()
    return msg_id


def _on_claim(monkeypatch, msg_id: str, action) -> None:
    """Run `action` right after the claim of `msg_id` succeeds: a delay or a concurrent change
    that lands between the claim and the spawn."""
    real = outbox.claim

    def claim(conn, item_id, **kw):
        rid = real(conn, item_id, **kw)
        if rid and item_id == msg_id:
            action()
        return rid

    monkeypatch.setattr(outbox, "claim", claim)


def _snapshots(root):
    directory = root / sender.SNAPSHOTS_REL
    return list(directory.iterdir()) if directory.exists() else []


def test_a_claim_delay_crossing_expiry_never_transmits(paired, monkeypatch):
    devices, wire, *_ = paired
    old = _open_row(devices.a, expires_in=1)
    later = (now() + timedelta(seconds=5)).isoformat()
    _on_claim(monkeypatch, old, lambda: monkeypatch.setenv("AGENT_NOW", later))
    result = _send(devices.a)
    assert {"id": old, "outcome": "acceptance-unknown"} in result["earlier"]
    assert [c["fault"] for c in wire.calls] == ["ok"]  # only the new message went out


def test_a_claim_delay_crossing_the_deadline_never_transmits(paired, monkeypatch):
    devices, wire, *_ = paired
    _on_claim(monkeypatch, "*", lambda: None)
    real = outbox.claim

    def slow_claim(conn, item_id, **kw):
        rid = real(conn, item_id, **kw)
        time.sleep(1.2)
        return rid

    monkeypatch.setattr(outbox, "claim", slow_claim)
    result = _send(devices.a, clock=Clock(1.5))
    assert result["outcome"] in {"unconfirmed", "uncertain"}
    assert wire.calls == []


def test_store_guard_contention_is_bounded_by_the_send_budget(paired):
    devices, *_ = paired
    from agent_cli.dispatch.guards import area_guard

    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with area_guard(devices.a, "peer-net:store-init"):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    started = time.monotonic()
    try:
        with pytest.raises(PeerNetError) as caught:
            _send(devices.a, clock=Clock(0.6))
    finally:
        release.set()
        holder.join(5)
    assert caught.value.code == "timeout" and time.monotonic() - started < 2.0


def test_a_pin_replaced_after_the_claim_abandons_without_sending(paired, monkeypatch):
    devices, wire, *_ = paired
    old = _open_row(devices.a)
    pin = ssh.pin_path(devices.a, "mac")
    _on_claim(monkeypatch, old, lambda: pin.write_text(f"agent-peer-mac {host_line(77)}\n"))
    result = _send(devices.a)
    assert {"id": old, "outcome": "abandoned:route-changed"} in result["earlier"]
    assert [c["fault"] for c in wire.calls] == ["ok"] and _snapshots(devices.a) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege the Windows test user lacks")
def test_a_linked_pin_is_never_followed(paired, monkeypatch, tmp_path):
    devices, wire, *_ = paired
    pin = ssh.pin_path(devices.a, "mac")
    foreign = tmp_path / "foreign"
    foreign.write_bytes(pin.read_bytes())
    pin.unlink()
    pin.symlink_to(foreign)
    result = _send(devices.a)
    assert result["outcome"] == "unconfirmed" and wire.calls == [] and _snapshots(devices.a) == []


def test_a_route_changed_after_the_claim_abandons_without_sending(paired, monkeypatch):
    devices, wire, *_ = paired
    old = _open_row(devices.a)

    def change() -> None:
        conn = store.connect(devices.a)
        try:
            with store.transaction(conn):
                conn.execute("UPDATE outbound SET user='someone-else' WHERE alias='mac'")
        finally:
            conn.close()

    _on_claim(monkeypatch, old, change)
    result = _send(devices.a)  # the new message is journaled with the then-current route
    assert {"id": old, "outcome": "abandoned:route-changed"} in result["earlier"]


def test_ssh_trusts_a_private_snapshot_and_the_snapshot_is_removed(paired, monkeypatch):
    devices, wire, *_ = paired
    seen = []
    real_run = ssh.run_bounded

    def spy(command, stdin, **kw):
        option = next(c for c in command if c.startswith("UserKnownHostsFile="))
        path = option.split("=", 1)[1].strip('"')
        others = 0 if os.name == "nt" else os.stat(path).st_mode & 0o077  # no POSIX mode bits on Windows
        seen.append((path, open(path, "rb").read(), others))
        return real_run(command, stdin, **kw)

    monkeypatch.setattr(ssh, "run_bounded", spy)
    _send(devices.a)
    (path, data, others), *_ = seen
    assert path != str(ssh.pin_path(devices.a, "mac")) and others == 0
    assert data == ssh.pin_path(devices.a, "mac").read_bytes()
    assert _snapshots(devices.a) == []


def test_a_lost_settle_reports_the_durable_state_not_its_own(paired, monkeypatch):
    devices, wire, *_ = paired
    real = outbox.settle

    def contested(conn, msg_id, rid, state, outcome, **kw):
        # Another attempt whose claim won meanwhile settled the row first.
        with store.transaction(conn):
            conn.execute(
                "UPDATE outbox SET state='refused:disabled', claim_rid='other', outcome='{}' WHERE id=?",
                (msg_id,),
            )
        return real(conn, msg_id, rid, state, outcome, **kw)

    monkeypatch.setattr(outbox, "settle", contested)
    result = _send(devices.a)
    assert result["outcome"] == "refused:disabled" and result["answer"] is None


def test_close_reports_skipped_when_another_attempt_holds_the_row(paired):
    devices, *_ = paired
    old = _open_row(devices.a, expires_in=-1)
    conn = store.connect(devices.a)
    try:
        assert outbox.claim(conn, old, lease_s=600, now=sender._now())
        item = outbox.get(conn, old)
        assert item is not None
        assert sender._close(conn, item, "acceptance-unknown", "x", Clock(5)) == "skipped"
        assert outbox.get(conn, old).state == "pending"  # type: ignore[union-attr]
    finally:
        conn.close()


def test_a_settle_that_cannot_be_recorded_is_uncertain_with_its_id(paired, monkeypatch):
    devices, *_ = paired

    def unavailable(*args, **kwargs):
        raise PeerNetError("store-unavailable", "store")

    monkeypatch.setattr(outbox, "settle", unavailable)
    result = _send(devices.a)
    assert result["outcome"] == "uncertain" and len(result["id"]) == 32


ACK = {
    "v": 1,
    "ok": True,
    "state": "accepted",
    "queue": "queued",
    "wake": "accepted",
    "recovery": "none",
    "id": "b" * 32,
    "to_name": "mgr-b",
}


@pytest.mark.parametrize(
    "over",
    [
        {"state": []},
        {"queue": {}},
        {"wake": []},
        {"recovery": [1]},
        {"id": ["b" * 32]},
        {"to_name": "\ud800"},
        {"to_name": ["mgr-b"]},
    ],
)
def test_unhashable_or_invalid_fields_are_no_answer_not_a_crash(over):
    line = (json.dumps({**ACK, **over}) + "\n").encode()
    assert ack.enqueue_answer(0, line, "b" * 32) is None


def test_deep_nesting_is_no_answer_not_a_crash():
    deep = b'{"v": 1, "ok": true, "state": ' + b"[" * 3000 + b"]" * 3000 + b"}\n"
    assert ack.enqueue_answer(0, deep, "b" * 32) is None
    assert ack.list_answer(0, deep, "mac") == "no-answer"


def test_a_refusal_with_an_unhashable_code_is_no_answer():
    line = (json.dumps({"v": 1, "ok": False, "error": ["x"], "detail": "d"}) + "\n").encode()
    assert ack.enqueue_answer(1, line, "b" * 32) is None
