"""Two enrolled devices in one test process, for the pairing protocol.

Device A (`win`) runs `pair` in the test's own thread; device B (`mac`) runs `accept` in a
thread named "B". Each has its own harness root, authorized_keys file and sshd host key; OS
facts are answered per thread. A probe is the real receiver on the other root, so "verified"
needs the other side enabled and its grant active, exactly as over SSH.
"""

from __future__ import annotations

import base64
import functools
import json
import queue
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_cli.core import now
from agent_cli.peer_net import (
    authorized,
    enroll,
    intents,
    launcher,
    pairing,
    receive,
    routes,
    ssh,
    store,
    system,
)
from agent_cli.peer_net.errors import PeerNetError

NAME = b"ssh-ed25519"
# How much longer than its window a read waits while the other end is still working. Under
# full-suite load one step (a grant, a probe, an owner's hook) outlasts the 0.3 s POSIX window
# as well as the 1 s Windows one. A lost message still times out at the window: its sender is
# reading by then, so the wait ends at once.
PEER_WORKING_S = 3.0


def host_line(seed: int) -> str:
    blob = len(NAME).to_bytes(4, "big") + NAME + (32).to_bytes(4, "big") + bytes([seed] * 32)
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


_USER_SID = functools.cache(system.user_sid)  # one `whoami` per process, not per test
A_HOST, B_HOST = host_line(21), host_line(22)


def on_b() -> bool:
    return threading.current_thread().name == "B"


def as_b(call: Callable[[], Any]) -> Any:
    """Run `call` as device B: OS facts answered for B."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["ok"] = call()
        except Exception as exc:  # noqa: BLE001 -- re-raised in the test's thread
            box["error"] = exc

    thread = threading.Thread(target=run, name="B")
    thread.start()
    thread.join(10)
    if "error" in box:
        raise box["error"]
    return box.get("ok")


def crash_on(
    monkeypatch: pytest.MonkeyPatch, target: Any, name: str, side: str, *, after: bool = False
) -> None:
    """Make `target.name` die once on `side` ("A" or "B"), before or after it does its work:
    a power cut at that exact point. Later calls run normally."""
    real = getattr(target, name)
    fired: list[bool] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        mine = (side == "B") == on_b() and not fired
        if mine and not after:
            fired.append(True)
            raise RuntimeError("power cut")
        result = real(*args, **kwargs)
        if mine and after:
            fired.append(True)
            raise RuntimeError("power cut")
        return result

    monkeypatch.setattr(target, name, wrapper)


class Pipe:
    """One direction of a session. A phase in `drop` is lost once."""

    def __init__(self, drop: set[str]) -> None:
        self.q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.drop = drop
        self.delivered = 0
        self.taken = 0

    def put(self, message: dict[str, Any] | None) -> None:
        phase = None if message is None else str(message.get("phase"))
        if phase in self.drop:
            self.drop.discard(phase)
            return
        self.delivered += 1
        self.q.put(message)


class End:
    """One side's end, standing in for `pairing.LineChannel`: `recv` returns the next message,
    raises `pair-closed` once the other end has closed, or raises `pair-timeout`.

    Past its timeout a read waits on, for at most PEER_WORKING_S more, while the other end is
    still working on its reply rather than reading, closed, or waiting for the owner to log in,
    so a step slowed by a loaded machine is not taken for a lost message."""

    def __init__(
        self,
        inbox: Pipe,
        outbox: Pipe,
        crash: str | None = None,
        crash_send: str | None = None,
        on_recv: dict[str, Callable[[], Any]] | None = None,
    ) -> None:
        self.inbox, self.outbox, self.crash, self.crash_send = inbox, outbox, crash, crash_send
        self.on_recv = on_recv or {}
        self.sent_timeouts: list[float] = []
        self.peer: End | None = None
        self.reading = False
        self.closed = False
        self.idle = False

    def send(self, message: dict[str, Any], timeout: float) -> None:
        self.sent_timeouts.append(timeout)
        if message.get("phase") == self.crash_send:
            raise RuntimeError("B died")  # before the message leaves
        self.outbox.put(message)

    def _peer_silent(self) -> bool:
        peer = self.peer
        if peer is None or peer.closed or peer.idle:
            return True
        # A peer still flagged as reading may already hold our last message, popped but not yet
        # returned from its read. It is waiting on us only once it has taken all we sent; `taken`
        # is read before `reading` because the peer clears `reading` before it counts.
        return self.outbox.taken >= self.outbox.delivered and peer.reading

    def _take(self, block: bool, timeout: float | None = None) -> dict[str, Any] | None:
        message = self.inbox.q.get(block, timeout)
        self.reading = False
        self.inbox.taken += 1
        return message

    def _next(self, timeout: float) -> dict[str, Any] | None:
        self.reading = True
        try:
            try:
                return self._take(True, timeout)
            except queue.Empty:
                if not PEER_WORKING_S:
                    raise
            deadline = time.monotonic() + PEER_WORKING_S
            while time.monotonic() < deadline and not self._peer_silent():
                try:
                    return self._take(True, 0.02)
                except queue.Empty:
                    pass
            # The peer queues its message before it reads or closes, so this sees it.
            return self._take(False)
        except queue.Empty:
            raise PeerNetError("pair-timeout", "no reply in time") from None
        finally:
            self.reading = False

    def recv(self, timeout: float) -> dict[str, Any]:
        message = self._next(timeout)
        if message is None:
            raise PeerNetError("pair-closed", "closed")
        if message.get("phase") == "error":
            raise PeerNetError(str(message["code"]), str(message.get("detail")))
        if message.get("phase") == self.crash:
            raise RuntimeError("B died")  # the process is gone: nothing more is sent
        hook = self.on_recv.pop(str(message.get("phase")), None)
        if hook is not None:
            hook()  # the owner acts while this side stalls on the message
        return message

    def close(self) -> None:
        self.outbox.put(None)
        self.closed = True


class Devices:
    def __init__(self, a_root: Path, b_root: Path) -> None:
        self.a, self.b = a_root, b_root
        self.drop_to_b: set[str] = set()
        self.drop_to_a: set[str] = set()
        self.b_crash: str | None = None  # B dies on receiving this phase
        self.b_crash_send: str | None = None  # B dies as it would send this phase
        self.a_on_recv: dict[str, Callable[[], Any]] = {}  # run as A receives a phase
        self.b_on_recv: dict[str, Callable[[], Any]] = {}
        self.b_result: dict[str, Any] = {}
        self.b_thread: threading.Thread | None = None
        self.failing_probe: set[str] = set()  # aliases whose probe gets no answer
        self.probe_timeouts: list[float | None] = []
        self.login_s = 0.0  # how long the owner takes to log in

    def session(self) -> End:
        to_b, to_a = Pipe(self.drop_to_b), Pipe(self.drop_to_a)
        b_end = End(to_b, to_a, self.b_crash, self.b_crash_send, self.b_on_recv)
        a_end = End(to_a, to_b, on_recv=self.a_on_recv)
        a_end.peer, b_end.peer = b_end, a_end
        b_end.idle = self.login_s > 0

        def run() -> None:
            time.sleep(self.login_s)  # B's accept starts only once the owner has logged in
            b_end.idle = False
            try:
                self.b_result = {
                    "ok": pairing.accept(
                        self.b, b_end, env={"SSH_CONNECTION": "192.0.2.5 50000 192.0.2.9 22"}
                    )
                }
            except Exception as exc:  # noqa: BLE001 -- recorded for the test
                self.b_result = {"error": getattr(exc, "code", type(exc).__name__)}

        self.b_thread = threading.Thread(target=run, name="B", daemon=True)
        self.b_thread.start()
        self.a_end, self.b_end = a_end, b_end
        return self.a_end

    def pair(
        self,
        *,
        b_alias: str = "mac",
        enable: bool = True,
        restart: bool = False,
        confirm: bool = True,
        login_timeout_s: float = 0.0,
    ) -> dict[str, Any]:
        self.b_thread, self.b_result = None, {}
        try:
            return pairing.pair(
                self.a,
                b_alias=b_alias,
                user="user",
                host="192.0.2.9",
                port=22,
                device="win",
                enable=enable,
                restart=restart,
                confirm=lambda fp: confirm,
                session=self.session,
                self_user="user",
                login_timeout_s=login_timeout_s,
            )
        finally:
            if self.b_thread is not None:
                self.b_thread.join(10)

    def pair_error(self, **kwargs: Any) -> str:
        with pytest.raises(PeerNetError) as caught:
            self.pair(**kwargs)
        return caught.value.code

    def _row(self, root: Path, sql: str, alias: str) -> tuple | None:
        conn = store.connect(root)
        try:
            return conn.execute(sql, (alias,)).fetchone()
        finally:
            conn.close()

    def grant(self, root: Path, alias: str) -> str | None:
        row = self._row(root, "SELECT state FROM inbound WHERE alias=? AND revoked=0", alias)
        return None if row is None else row[0]

    def route(self, root: Path, alias: str) -> tuple | None:
        return self._row(root, "SELECT host, verified FROM outbound WHERE alias=?", alias)

    def enabled(self, root: Path) -> bool:
        return self._row(root, "SELECT v FROM enrollment WHERE k=?", "enabled") == ("1",)

    def open_intents(self, root: Path) -> list[intents.Intent]:
        conn = store.connect(root)
        try:
            return intents.open_for(conn)
        finally:
            conn.close()


def make_devices(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Devices:
    home = tmp_path / "home"
    home.mkdir()
    b_root = tmp_path / "b-harness"
    b_root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows reads home from here, not HOME
    monkeypatch.setattr(pairing, "MESSAGE_TIMEOUT_S", 1.0 if sys.platform == "win32" else 0.3)
    if sys.platform == "win32":
        # Every ACL check or grant is a PowerShell or icacls spawn of about 0.9 s, longer than
        # the message window; the real ACL checks are covered by test_peer_net_acl_errors and _recover.
        monkeypatch.setattr(system, "is_private", lambda path: True)
        monkeypatch.setattr(system, "user_sid", _USER_SID)
        monkeypatch.setattr(system, "_icacls", lambda path, *grants: None)
    keys = {"A": home / "a_authorized_keys", "B": home / "b_authorized_keys"}
    monkeypatch.setattr(system, "authorized_file", lambda: (keys["B" if on_b() else "A"], False))
    monkeypatch.setattr(system, "host_key_line", lambda: B_HOST if on_b() else A_HOST)
    monkeypatch.setattr(
        system,
        "host_fingerprints",
        lambda: [authorized.fingerprint(authorized.ed25519_blob(B_HOST if on_b() else A_HOST))],
    )
    monkeypatch.setattr(
        launcher,
        "resolve",
        lambda root: launcher.Launch("/usr/bin/python3", "/srv/agent/bin/peer-net-receive", "posix"),
    )
    monkeypatch.setattr(ssh, "scan", lambda host, port: [B_HOST])
    devices = Devices(tmp_root, b_root)

    def probe_status(
        root: Path, *, device: str, alias: str, user: str, host: str, port: int, timeout: float | None = None
    ) -> dict[str, Any]:
        devices.probe_timeouts.append(timeout)
        if alias in devices.failing_probe:
            return {"ok": False, "error": "no-answer"}
        at = now()
        body = {
            "v": 1,
            "verb": "status",
            "id": uuid.uuid4().hex,
            "sent_at": at.isoformat(),
            "expires_at": (at + timedelta(seconds=60)).isoformat(),
        }
        return receive(b_root if alias == "mac" else tmp_root, device, json.dumps(body).encode())

    monkeypatch.setattr(routes, "probe_status", probe_status)
    enroll.init(tmp_root, "win")
    as_b(lambda: enroll.init(b_root, "mac"))
    return devices
