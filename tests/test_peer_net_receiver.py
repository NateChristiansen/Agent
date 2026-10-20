"""The cross-device receiver accepts, queues and wakes once, and says only what it knows."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agent_cli import peers, sessions
from agent_cli.peer_net import receive
from agent_cli.peer_net import store as store_mod
from peer_net_helpers import NOW, clock, enable, envelope, raw, recipient


@pytest.fixture
def rings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)

    def fake_wake(target, name, codex_launch=None):
        calls.append(str(target.get("session_id") or name))
        return True, "rang"

    monkeypatch.setattr("agent_cli.ops.peers.wake_peer", fake_wake)
    return calls


@pytest.fixture
def no_queue(monkeypatch: pytest.MonkeyPatch):
    """While `no_queue["on"]`, acceptance commits but the append never happens: the state a
    crash between accept and queue leaves."""
    from agent_cli.peer_net import receiver

    real = receiver._queue
    flag = {"on": True}
    monkeypatch.setattr(receiver, "_queue", lambda *a, **k: False if flag["on"] else real(*a, **k))
    return flag


def _send(root: Path, body: dict, device: str = "mac", at=NOW) -> dict:
    return receive(root, device, raw(body), clock=clock(at))


def test_a_new_message_is_accepted_queued_and_woken_with_remote_provenance(tmp_root, rings):
    enable(tmp_root)
    sid = recipient(tmp_root)
    body = envelope()
    out = _send(tmp_root, body)
    assert out == {
        "v": 1,
        "ok": True,
        "state": "accepted",
        "queue": "queued",
        "wake": "accepted",
        "recovery": "none",
        "id": body["id"],
        "to_name": "mgr-recipient",
    }
    [row] = peers.peek(tmp_root, sid)
    assert row["from"] == "mac/098b0000" and row["from_name"] == "mac/mgr-001"
    assert row["remote"] == {"device": "mac", "msg_id": body["id"]}
    assert "sender's claim" in peers.block([row])
    assert rings == [sid]


def test_a_lost_ack_retry_adds_no_second_row_and_no_second_wake(tmp_root, rings):
    enable(tmp_root)
    sid = recipient(tmp_root)
    body = envelope()
    _send(tmp_root, body)
    again = _send(tmp_root, body)
    assert again["state"] == "already-accepted" and again["queue"] == "queued"
    assert again["recovery"] == "none" and again["wake"] == "accepted"
    assert len(peers.peek(tmp_root, sid)) == 1 and rings == [sid]


def test_a_retry_after_the_recipient_drained_does_not_append_again(tmp_root, rings):
    enable(tmp_root)
    sid = recipient(tmp_root)
    body = envelope()
    _send(tmp_root, body)
    peers.take(tmp_root, sid)
    assert _send(tmp_root, body)["state"] == "already-accepted"
    assert peers.peek(tmp_root, sid) == []


def test_a_reused_id_with_other_content_is_a_conflict(tmp_root, rings):
    enable(tmp_root)
    recipient(tmp_root)
    body = envelope()
    _send(tmp_root, body)
    assert _send(tmp_root, {**body, "text": "something else"})["error"] == "conflict"


def test_the_same_id_from_two_devices_is_two_messages(tmp_root, rings):
    enable(tmp_root, devices={"mac": {}, "laptop": {}})
    sid = recipient(tmp_root)
    body = envelope()
    assert _send(tmp_root, body, "mac")["state"] == "accepted"
    assert _send(tmp_root, body, "laptop")["state"] == "accepted"
    assert {r["remote"]["device"] for r in peers.peek(tmp_root, sid)} == {"mac", "laptop"}


def test_the_target_is_frozen_at_acceptance(tmp_root, rings, no_queue):
    """A name reused by another session after acceptance never redirects a retry."""
    enable(tmp_root)
    first = recipient(tmp_root)
    body = envelope()
    conn = store_mod.connect(tmp_root)
    _send(tmp_root, body)  # accepted, never queued
    no_queue["on"] = False
    sessions.set_record(tmp_root, first, name="renamed")
    recipient(tmp_root, sid="cx-other", name="mgr-recipient")
    _send(tmp_root, body)
    row = store_mod.lookup(conn, "mac", body["id"])
    assert row is not None and row.target_sid == first
    assert len(peers.peek(tmp_root, first)) == 1 and peers.peek(tmp_root, "cx-other") == []


def test_the_sender_device_comes_from_the_key_never_the_envelope(tmp_root, rings):
    enable(tmp_root)
    sid = recipient(tmp_root)
    assert _send(tmp_root, envelope(device="windows"))["error"] == "unknown-key"
    _send(tmp_root, envelope(from_session="local-manager", from_name="mgr-local"))
    [row] = peers.peek(tmp_root, sid)
    assert row["from"].startswith("mac/") and row["remote"]["device"] == "mac"


def test_an_unknown_or_revoked_device_is_refused(tmp_root, rings):
    enable(tmp_root, devices={"mac": {}, "old": {"revoked": True}})
    recipient(tmp_root)
    assert _send(tmp_root, envelope(), "old")["error"] == "unknown-device"
    assert _send(tmp_root, envelope(), "stranger")["error"] == "unknown-device"
    assert _send(tmp_root, envelope(), "Mac/../x")["error"] == "unknown-device"


def test_off_by_default_and_the_off_switch_keeps_the_queue(tmp_root, rings):
    sid = recipient(tmp_root)
    assert _send(tmp_root, envelope())["error"] == "disabled"  # no peer_net block at all
    enable(tmp_root)
    _send(tmp_root, envelope())
    enable(tmp_root, enabled=False)
    assert _send(tmp_root, envelope())["error"] == "disabled"
    assert len(peers.peek(tmp_root, sid)) == 1


def test_an_expired_new_message_is_refused_but_a_known_one_still_acks(tmp_root, rings):
    enable(tmp_root)
    sid = recipient(tmp_root)
    late = NOW + timedelta(minutes=16)
    assert _send(tmp_root, envelope(), at=late)["error"] == "expired"
    body = envelope()
    _send(tmp_root, body)
    peers.take(tmp_root, sid)
    out = _send(tmp_root, body, at=late)  # past its window: an ack of history, nothing more
    assert out["state"] == "already-accepted" and out["recovery"] == "none" and out["wake"] == "accepted"
    assert peers.peek(tmp_root, sid) == []


def test_an_unqueued_message_is_not_recovered_after_its_window(tmp_root, rings, no_queue):
    enable(tmp_root)
    sid = recipient(tmp_root)
    body = envelope()
    assert _send(tmp_root, body)["queue"] == "pending"
    no_queue["on"] = False
    after = NOW + timedelta(minutes=10, seconds=301)
    out = _send(tmp_root, body, at=after)
    assert out["state"] == "already-accepted" and out["queue"] == "pending" and out["recovery"] == "none"
    assert peers.peek(tmp_root, sid) == []


def test_the_rate_limit_counts_new_ids_only(tmp_root, rings):
    enable(tmp_root)
    recipient(tmp_root)
    first = envelope()
    for i in range(store_mod.RATE_LIMIT):
        assert _send(tmp_root, first if i == 0 else envelope())["ok"]
    assert _send(tmp_root, envelope())["error"] == "rate-limited"
    assert _send(tmp_root, first)["state"] == "already-accepted"  # a duplicate needs no token
    assert _send(tmp_root, envelope(), at=NOW + timedelta(seconds=601))["ok"]


def test_an_unknown_or_ambiguous_recipient_is_refused(tmp_root, rings):
    enable(tmp_root)
    recipient(tmp_root, sid="aaaa1111", name="mgr-one")
    recipient(tmp_root, sid="bbbb1111", name="mgr-one")
    assert _send(tmp_root, envelope(to="mgr-one"))["error"] == "ambiguous-recipient"
    assert _send(tmp_root, envelope(to="mgr-none"))["error"] == "unknown-recipient"
    assert _send(tmp_root, envelope(to="aaaa11"))["error"] == "unknown-recipient"  # no id prefix


def test_a_former_name_reaches_its_session_only_when_unique(tmp_root, rings):
    """A sender holding a manager's pre-remap name was refused as unknown."""
    enable(tmp_root)
    sid = recipient(tmp_root, sid="cccc1111", name="mgr-041-config-save-relink")
    sessions.set_field(tmp_root, sid, "former_names", ["mgr-unmapped-cccc11"])
    out = _send(tmp_root, envelope(to="mgr-unmapped-cccc11"))
    assert out["ok"] and out["to_name"] == "mgr-041-config-save-relink"
    other = recipient(tmp_root, sid="dddd1111", name="mgr-042")
    sessions.set_field(tmp_root, other, "former_names", ["mgr-unmapped-cccc11"])
    assert _send(tmp_root, envelope(to="mgr-unmapped-cccc11"))["error"] == "ambiguous-recipient"


def test_list_exports_only_the_allowlisted_fields(tmp_root, rings):
    enable(tmp_root)
    recipient(tmp_root)
    body = {k: envelope()[k] for k in ("v", "id", "sent_at", "expires_at")}
    out = _send(tmp_root, {**body, "verb": "list"})
    assert out["ok"] and out["device"] == "windows"
    for session in out["sessions"]:
        assert set(session) == {"name", "tab", "provider", "feature", "state"}
    assert "session_id" not in str(out) and str(tmp_root) not in str(out)


def test_status_names_the_device(tmp_root, rings):
    enable(tmp_root)
    body = {k: envelope()[k] for k in ("v", "id", "sent_at", "expires_at")}
    assert _send(tmp_root, {**body, "verb": "status"}) == {
        "v": 1,
        "ok": True,
        "device": "windows",
        "enabled": True,
        "proto": 1,
    }


def test_errors_never_echo_input_or_paths(tmp_root, rings, monkeypatch):
    enable(tmp_root)
    recipient(tmp_root)

    def broken(root):
        raise OSError(f"cannot open {root}/secret")

    monkeypatch.setattr("agent_cli.peer_net.store.connect", broken)
    out = _send(tmp_root, envelope(text="SECRET-TEXT"))
    assert out == {"v": 1, "ok": False, "error": "internal", "detail": "receiver"}


def test_retention_prunes_old_rows(tmp_root, rings):
    enable(tmp_root)
    recipient(tmp_root)
    old = envelope()
    _send(tmp_root, old)
    later = NOW + timedelta(days=2)
    _send(
        tmp_root,
        envelope(sent_at=later.isoformat(), expires_at=(later + timedelta(minutes=5)).isoformat()),
        at=later,
    )
    assert store_mod.lookup(store_mod.connect(tmp_root), "mac", old["id"]) is None
