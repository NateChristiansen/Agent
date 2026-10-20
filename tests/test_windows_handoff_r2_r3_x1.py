"""Windows handoff: queued wake fallback."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from agent_cli import ops, peers, sessions


def test_missing_af_unix_reports_failure_and_keeps_peer_message(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    sessions.set_record(tmp_root, "sender", pid=101, name="mgr-sender")
    sessions.set_record(tmp_root, "recipient", pid=102, name="mgr-recipient")
    sessions.set_field(tmp_root, "recipient", "inbox", "/tmp/recipient-inbox.sock")  # noqa: S108 — never opened
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)

    sent = ops.peer_send(tmp_root, to="mgr-recipient", message="Windows finding", session_id="sender")

    assert "queued for mgr-recipient" in sent.text
    assert "Unix inbox sockets are unavailable" in sent.text
    assert "next turn" in sent.text
    assert [message["text"] for message in peers.peek(tmp_root, "recipient")] == ["Windows finding"]
    assert (sessions.get(tmp_root, "recipient") or {})[peers.WAKE_FIELD]["state"] == "failed"
