"""A Claude inbox on Windows is a named pipe with a random name, and it drops a connection that
does not open with the recipient's peer token. The owner's second Windows run
showed Claude-to-Codex wake working and Codex-to-Claude wake failing with "Unix inbox sockets are
unavailable on this platform". The hook now records the address; the sender reads the token
from Claude Code's key file at wake time and never keeps, logs, or reports it."""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from agent_cli import hooks, ops, sessions

TOKEN = "peer-token-that-must-not-leak"
PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-0123456789abcdef0123456789abcdef"


def _key(config: Path, pid: int, canonical: str, body: str | None = None) -> Path:
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    path = config / "sessions" / f"{pid}.{digest}.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = body if body is not None else json.dumps({"peerToken": TOKEN, "procStart": "x", "pidDomain": "y"})
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path / "claude"


def test_the_token_comes_from_the_key_named_by_the_address_hash(config: Path):
    _key(config, 20337, "/tmp/cc-socks/20337.sock")
    assert ops.peers.inbox_token("/tmp/cc-socks/20337.sock") == TOKEN
    assert ops.peers.inbox_token("/tmp/cc-socks/20338.sock") is None


def test_a_pipe_name_hashes_lowercased(config: Path):
    """Windows compares pipe names case-insensitively, and Claude Code hashes the lowercased
    name; the `\\\\.\\pipe\\` prefix itself is kept as written."""
    _key(config, 9120, PIPE.lower())
    assert ops.peers.inbox_token(PIPE.replace("cc-msg", "CC-MSG")) == TOKEN


def test_an_unreadable_key_is_no_token_and_no_content(config: Path):
    _key(config, 1, "/tmp/a.sock", body=f"not json {TOKEN}")
    assert ops.peers.inbox_token("/tmp/a.sock") is None


def test_a_bad_key_does_not_hide_a_good_one_with_the_same_hash(config: Path):
    """A stale key from an earlier process on the same address shares the hash."""
    _key(config, 1, "/tmp/a.sock", body="not json")
    _key(config, 2, "/tmp/a.sock")
    assert ops.peers.inbox_token("/tmp/a.sock") == TOKEN


def test_a_pipe_that_never_answers_is_a_bounded_failure(config: Path, monkeypatch: pytest.MonkeyPatch):
    """`open` on a pipe has no timeout of its own; a peer send must not hang on it."""
    import threading

    _key(config, 9120, PIPE.lower())
    release = threading.Event()
    monkeypatch.setattr(ops.peers, "WAKE_TIMEOUT_S", 0.2)

    def hang(*args, **kwargs):
        release.wait(5)
        raise OSError("closed after the test")

    monkeypatch.setattr("builtins.open", hang)
    try:
        error = ops.peers._ring_socket(PIPE)
    finally:
        release.set()
    assert error is not None and error.startswith("TimeoutError")
    assert TOKEN not in error


def test_a_pipe_without_a_key_is_not_opened(config: Path, monkeypatch: pytest.MonkeyPatch):
    opened: list = []
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a))
    assert ops.peers._ring_socket(PIPE) == "no readable inbox key for this pipe"
    assert opened == []


def test_a_pipe_gets_the_auth_line_then_the_frame(config: Path, monkeypatch: pytest.MonkeyPatch):
    _key(config, 9120, PIPE.lower())
    written: list[bytes] = []

    class Pipe:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data: bytes) -> None:
            written.append(data)

    monkeypatch.setattr("builtins.open", lambda *a, **k: Pipe())
    assert ops.peers._ring_socket(PIPE) is None
    auth, frame = written[0].decode().splitlines()
    assert json.loads(auth) == {"type": "auth", "token": TOKEN}
    assert json.loads(frame)["message"]["content"] == ops.peers.WAKE_TEXT


@pytest.mark.skipif(sys.platform == "win32", reason="a Unix inbox socket under /tmp")
def test_a_unix_inbox_gets_the_auth_line_when_a_key_exists(config: Path):
    with tempfile.TemporaryDirectory(dir="/tmp") as short:  # noqa: S108 — AF_UNIX path length
        address = str(Path(short) / "inbox.sock")
        _key(config, 5, address)
        got: list[bytes] = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(address)
            server.listen(1)

            def accept() -> None:
                conn, _ = server.accept()
                with conn:
                    got.append(conn.recv(4096))

            listener = threading.Thread(target=accept)
            listener.start()
            assert ops.peers._ring_socket(address) is None
            listener.join(timeout=5)
    lines = got[0].decode().splitlines()
    assert json.loads(lines[0]) == {"type": "auth", "token": TOKEN}
    assert json.loads(lines[1])["type"] == "user"


def test_the_token_never_reaches_a_result(tmp_root: Path, config: Path, monkeypatch: pytest.MonkeyPatch):
    """Nothing listens, so the ring fails; its reason names the address and the error only."""
    address = "/tmp/agent-test-nobody-home.sock"  # noqa: S108 — never bound
    _key(config, 424243, address)
    monkeypatch.setattr(ops.peers, "is_alive", lambda pid: pid == 424243)
    sessions.set_record(tmp_root, "sender", pid=424243, name="mgr-sender")
    sessions.set_record(tmp_root, "recipient", pid=424243, name="mgr-recipient")
    sessions.set_field(tmp_root, "recipient", "inbox", address)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sender")
    sent = ops.peer_send(tmp_root, to="mgr-recipient", message="hello")
    assert address in sent.text
    assert TOKEN not in json.dumps(sent.data) + sent.text
    assert TOKEN not in json.dumps(sessions.get(tmp_root, "recipient"))


def test_the_recorded_inbox_wins_over_the_pid_path(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    rung: list = []
    monkeypatch.setattr(ops.peers, "_ring_socket", lambda address: rung.append(address))
    target = {"session_id": "r", "pid": 7, "inbox": PIPE}
    woke, why = ops.peers.wake_peer(target, "mgr-r")
    assert woke
    assert rung == [PIPE]
    assert "inbox pipe" in why  # not "socket"


def test_windows_without_a_recorded_inbox_says_so(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ops.peers.sys, "platform", "win32")
    woke, why = ops.peers.wake_peer({"session_id": "r", "pid": 7}, "mgr-r")
    assert not woke
    assert "no inbox address recorded" in why


def test_the_activity_hook_records_the_inbox(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", PIPE)
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", TOKEN)
    sessions.set_record(tmp_root, "s1", name="mgr-s1")
    payload = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
    _, code = hooks.run(tmp_root, "activity", json.dumps(payload))
    assert code == 0
    record = sessions.get(tmp_root, "s1") or {}
    assert record.get("inbox") == PIPE
    assert TOKEN not in json.dumps(record)
