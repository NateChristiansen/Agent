"""The receiver's wake honours expiry after every wait, and a coalesced wake stays retryable.

From the paired review of the first receiver diff (2026-10-03): a ring could go out after its
window if the claim or the inbox guard was slow, and `wake_coalesced` was terminal even when the
reservation it reused later failed.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from agent_cli import peers, sessions
from agent_cli.ops import peer_wake
from agent_cli.peer_net import receive
from agent_cli.peer_net import store as store_mod
from peer_net_helpers import NOW, enable, envelope, raw, recipient

LATE = NOW + timedelta(minutes=16)  # past expires_at (NOW + 10 min) plus the 300 s skew


@pytest.fixture
def rings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)

    def fake_wake(target, name, codex_launch=None):
        calls.append(name)
        return True, "rang"

    monkeypatch.setattr("agent_cli.ops.peers.wake_peer", fake_wake)
    return calls


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self):
        return self.at


def _reservation(root: Path, sid: str) -> dict:
    return (sessions.get(root, sid) or {}).get(peers.WAKE_FIELD) or {}


def test_expiry_during_the_claim_wait_rings_nothing(tmp_root, rings, monkeypatch):
    enable(tmp_root)
    sid = recipient(tmp_root)
    clock = Clock()
    real = store_mod.claim_wake

    def slow_claim(*a, **k):
        claimed = real(*a, **k)
        clock.at = LATE  # BEGIN IMMEDIATE waited past the window
        return claimed

    monkeypatch.setattr(store_mod, "claim_wake", slow_claim)
    out = receive(tmp_root, "mac", raw(envelope()), clock=clock)
    assert out["wake"] == "expired" and rings == [] and _reservation(tmp_root, sid) == {}


def test_expiry_during_the_inbox_guard_wait_rings_nothing(tmp_root, rings, monkeypatch):
    enable(tmp_root)
    sid = recipient(tmp_root)
    clock = Clock()
    real = peers.inbox_guard
    real_queued = store_mod.mark_queued
    state = {"queued": False}

    def queued(*a, **k):
        real_queued(*a, **k)
        state["queued"] = True

    def slow_guard(root, target):
        if state["queued"]:  # the ring's guard, after the append finished
            clock.at = LATE
        return real(root, target)

    monkeypatch.setattr(store_mod, "mark_queued", queued)
    monkeypatch.setattr(peers, "inbox_guard", slow_guard)
    out = receive(tmp_root, "mac", raw(envelope()), clock=clock)
    assert out["wake"] == "expired" and rings == [] and _reservation(tmp_root, sid) == {}


def test_expiry_after_the_reservation_releases_it_and_rings_nothing(tmp_root, rings, monkeypatch):
    enable(tmp_root)
    sid = recipient(tmp_root)
    clock = Clock()
    real = peer_wake.reserve

    def slow_reserve(root, target):
        wake_id = real(root, target)
        clock.at = LATE
        return wake_id

    monkeypatch.setattr(peer_wake, "reserve", slow_reserve)
    out = receive(tmp_root, "mac", raw(envelope()), clock=clock)
    assert out["wake"] == "expired" and rings == []
    assert not peer_wake.wake_current(_reservation(tmp_root, sid))  # no live reservation left


def test_a_coalesced_wake_rings_on_retry_once_its_reservation_failed(tmp_root, rings):
    enable(tmp_root)
    sid = recipient(tmp_root)
    sessions.set_field(
        tmp_root, sid, peers.WAKE_FIELD, {"id": "local", "state": "attempting", "at": NOW.timestamp()}
    )
    body = envelope()
    first = receive(tmp_root, "mac", raw(body), clock=lambda: NOW)
    assert first["wake"] == "coalesced" and rings == []
    clean = receive(tmp_root, "mac", raw(body), clock=lambda: NOW)
    assert clean["wake"] == "coalesced" and clean["recovery"] == "none" and rings == []
    peer_wake.settle(tmp_root, sid, "local", False)  # the local sender's ring failed
    again = receive(tmp_root, "mac", raw(body), clock=lambda: NOW)
    assert again["wake"] == "accepted" and again["recovery"] == "wake_requested" and len(rings) == 1


def test_a_clean_retry_after_a_coalesced_wake_was_drained_rings_nothing(tmp_root, rings):
    """The shared wake worked and the hook took the message; an identical retry must not ring."""
    enable(tmp_root)
    sid = recipient(tmp_root)
    sessions.set_field(
        tmp_root, sid, peers.WAKE_FIELD, {"id": "local", "state": "accepted", "at": NOW.timestamp()}
    )
    body = envelope()
    assert receive(tmp_root, "mac", raw(body), clock=lambda: NOW)["wake"] == "coalesced"
    assert len(peers.take(tmp_root, sid, acknowledge_wake=True)) == 1  # clears the reservation
    again = receive(tmp_root, "mac", raw(body), clock=lambda: NOW)
    assert again["wake"] == "skipped" and again["recovery"] == "none" and rings == []
    assert _reservation(tmp_root, sid) == {}  # no fresh reservation installed
    assert receive(tmp_root, "mac", raw(body), clock=lambda: NOW)["wake"] == "skipped" and rings == []


def test_status_and_list_refuse_an_expired_envelope(tmp_root, rings):
    enable(tmp_root)
    common = {k: envelope()[k] for k in ("v", "id", "sent_at", "expires_at")}
    for verb in ("status", "list"):
        assert (
            receive(tmp_root, "mac", raw({**common, "verb": verb}), clock=lambda: LATE)["error"] == "expired"
        )


def test_blank_text_is_refused_before_acceptance_takes_a_token(tmp_root, rings):
    enable(tmp_root)
    recipient(tmp_root)
    assert receive(tmp_root, "mac", raw(envelope(text="   \n\t")), clock=lambda: NOW)["error"] == "bad-value"
    conn = store_mod.connect(tmp_root)
    assert conn.execute("SELECT COUNT(*) FROM rate").fetchone() == (0,)


def test_a_legacy_yaml_block_fails_closed_even_beside_a_valid_store(tmp_root, rings):
    enable(tmp_root)
    recipient(tmp_root)
    block = {"enabled": True, "device": "windows", "devices": {"mac": {}}}
    (tmp_root / "agent.local.yaml").write_text(json.dumps({"peer_net": block}), encoding="utf-8")
    assert receive(tmp_root, "mac", raw(envelope()), clock=lambda: NOW)["error"] == "config"


@pytest.mark.parametrize(("key", "value"), [("enabled", "yes"), ("device", "Bad Alias")])
def test_a_malformed_enrollment_value_is_refused_not_coerced(tmp_root, rings, key, value):
    enable(tmp_root)
    recipient(tmp_root)
    conn = store_mod.connect(tmp_root)
    with store_mod.transaction(conn):
        conn.execute("UPDATE enrollment SET v=? WHERE k=?", (value, key))
    assert receive(tmp_root, "mac", raw(envelope()), clock=lambda: NOW)["error"] == "config"


def test_pending_grants_are_not_admitted(tmp_root, rings):
    """An authorize that died between writing the key line and activating it stays refused."""
    enable(tmp_root)
    recipient(tmp_root)
    conn = store_mod.connect(tmp_root)
    with store_mod.transaction(conn):
        conn.execute("UPDATE inbound SET state='pending' WHERE alias='mac'")
    assert receive(tmp_root, "mac", raw(envelope()), clock=lambda: NOW)["error"] == "unknown-device"


def test_a_device_that_never_enrolled_gets_no_store(tmp_root, rings):
    out = receive(tmp_root, "mac", raw(envelope()), clock=lambda: NOW)
    assert out["error"] == "disabled" and not store_mod.exists(tmp_root)
