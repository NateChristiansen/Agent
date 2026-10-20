"""Exercise mailbox and hook wiring with a test socket; this does not prove provider delivery."""

import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from agent_cli import hooks, ops, peers, sessions


@pytest.mark.skipif(
    sys.platform == "win32", reason="opens a real AF_UNIX listener; Windows inboxes are named pipes"
)
@pytest.mark.parametrize("state", ["idle", "working"])
def test_recipient_reads_message_after_socket_wake(tmp_root: Path, monkeypatch, request, state: str):
    # macOS limits AF_UNIX paths to 104 bytes; pytest's nested temp root exceeds it.
    socket_tmp = tempfile.TemporaryDirectory(prefix="peer-", dir="/tmp")
    request.addfinalizer(socket_tmp.cleanup)
    socket_dir = Path(socket_tmp.name)
    monkeypatch.setattr(ops.peers, "MESSAGING_SOCKET_DIR", socket_dir)
    monkeypatch.setattr(ops.peers, "is_alive", lambda pid: pid == 424243)
    sessions.set_record(tmp_root, "sender", pid=424243, name="mgr-sender")
    sessions.set_record(tmp_root, "recipient", pid=424243, name="mgr-recipient")
    sessions.set_field(tmp_root, "recipient", "activity", {"state": state})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sender")

    received: list[str] = []
    ready = threading.Event()

    def recipient() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_dir / "424243.sock"))
            server.listen(1)
            server.settimeout(3)
            ready.set()
            conn, _ = server.accept()
            with conn:
                received.append(conn.recv(1024).decode())
        output, code = hooks.run(tmp_root, "dispatch-inbox", json.dumps({"session_id": "recipient"}))
        assert code == 0
        received.append(json.loads(output)["hookSpecificOutput"]["additionalContext"])

    thread = threading.Thread(target=recipient)
    thread.start()
    assert ready.wait(3)
    sent = ops.peer_send(tmp_root, to="mgr-recipient", message="review is ready")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert "delivery unconfirmed" in sent.text
    # The wake frame carries the pointer, and this test is the reason the pointer changed: the
    # hook below drains the mailbox into the woken turn, so a wake that says "run bin/agent peer
    # inbox" sends the recipient to an empty box. Assert the constant, not a sentence.
    assert ops.peers.WAKE_TEXT in json.loads(received[0])["message"]["content"]
    assert "review is ready" in received[1]
    assert peers.peek(tmp_root, "recipient") == []
