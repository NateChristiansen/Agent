"""Peer wakes ring for what needs the recipient (ops/peer_wake_policy.py): decisions and questions
ring now, acknowledgements never ring, and with a deferral set informational messages wait."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import hooks, ops, peers, sessions
from agent_cli.ops import peer_wake_policy as policy


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Approved. Proceed with landing.", policy.ACTIONABLE),
        ("agree sha256:0f3a", policy.ACTIONABLE),
        ("Can you look at the plan", policy.INFORMATIONAL),
        ("Can you look at the plan?", policy.ACTIONABLE),
        ("Got it, will fix the test", policy.ACTIONABLE),
        ("please hold the merge", policy.ACTIONABLE),
        ("thanks, got it", policy.RECEIPT),
        ("ACK.", policy.RECEIPT),
        ("ok", policy.RECEIPT),
        ("👍", policy.RECEIPT),
        ("Status: suite running, three minutes left", policy.INFORMATIONAL),
    ],
)
def test_messages_are_classified_actionable_first(text: str, kind: str):
    assert policy.classify(text) == kind


@pytest.fixture
def two(tmp_root: Path, monkeypatch):
    import os

    rung: list[str] = []
    monkeypatch.setattr(
        ops.peers, "wake_peer", lambda target, name, *a, **k: (rung.append(name) or True, f"rang {name}")
    )
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    for sid in ("a", "b"):
        sessions.set_record(tmp_root, sid, name=f"mgr-{sid}", pid=os.getpid())
    return tmp_root, rung


def _defer(root: Path, seconds: int) -> None:
    reg = root / "agent.yaml"
    reg.write_text(reg.read_text() + f"peers:\n  defer_nonurgent_s: {seconds}\n")


def test_acknowledgements_never_ring_so_two_managers_cannot_loop(two):
    root, rung = two
    ops.peer_send(root, to="mgr-b", message="ack", session_id="a")
    ops.peer_send(root, to="mgr-a", message="thanks, got it", session_id="b")
    assert rung == []
    assert [m["text"] for m in peers.peek(root, "b")] == ["ack"]


def test_with_no_deferral_an_informational_message_rings_as_before(two):
    root, rung = two
    ops.peer_send(root, to="mgr-b", message="suite is running", session_id="a")
    assert rung == ["mgr-b"]


def test_a_deferred_message_waits_and_the_senders_stop_rings_it_once_due(two, monkeypatch):
    root, rung = two
    _defer(root, 300)
    sent = ops.peer_send(root, to="mgr-b", message="suite is running", session_id="a")
    assert rung == [] and "waits unrung" in sent.text
    assert policy.ring_expired(root, "a") == []  # not due yet
    rows = (sessions.get(root, "b") or {})[policy.DEFER_FIELD]
    sessions.set_field(root, "b", policy.DEFER_FIELD, [{**r, "at": r["at"] - 301} for r in rows])
    hooks.run(root, "lease-yield", json.dumps({"session_id": "a"}))
    assert rung == ["mgr-b"] and not (sessions.get(root, "b") or {}).get(policy.DEFER_FIELD)


def test_a_question_rings_now_and_carries_the_deferred_queue(two):
    root, rung = two
    _defer(root, 300)
    ops.peer_send(root, to="mgr-b", message="fyi: rebased", session_id="a")
    ops.peer_send(root, to="mgr-b", message="Does this plan work for you?", session_id="a")
    assert rung == ["mgr-b"] and not (sessions.get(root, "b") or {}).get(policy.DEFER_FIELD)
    assert len(peers.peek(root, "b")) == 2


def test_the_deferral_survives_a_restart_and_reading_the_inbox_clears_it(two):
    root, rung = two
    _defer(root, 300)
    ops.peer_send(root, to="mgr-b", message="fyi: rebased", session_id="a")
    assert (sessions.get(root, "b") or {}).get(policy.DEFER_FIELD)  # a new process reads it back
    out, _ = hooks.run(root, "dispatch-inbox", json.dumps({"session_id": "b", "prompt": "next"}))
    assert "rebased" in out and not (sessions.get(root, "b") or {}).get(policy.DEFER_FIELD)
