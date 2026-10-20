"""The one bootstrap SSH session `pair` opens to the other device.

It runs under the owner's own login and runs only `peer-net accept` there. Authentication is
the owner's password by default: no key can authenticate it, the peer keys included, so a
restricted peer key can never stand in for the owner and swap `accept` for the receiver.
`--login-key` names one existing owner key instead, refused when it is a peer or enrolled key.
ssh asks for the password on its own terminal prompt; the harness never reads, stores or logs
it, and stderr goes straight to the owner, never saved.

The process is bounded from spawn: a watchdog kills it at the session's total deadline, and
closing the session kills it if it has not exited, so a blocked read or write in the channel
always ends with the process (the limit `channel` states).
"""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from pathlib import Path

from . import authorized, ssh
from .channel import LineChannel
from .errors import PeerNetError

LOGIN_TIMEOUT_S = 300.0  # from spawn, covers the interactive login
EXIT_GRACE_S = 5.0

PASSWORD_ONLY = {
    "PubkeyAuthentication": "no",
    "IdentityAgent": "none",
    "IdentitiesOnly": "yes",
    "PreferredAuthentications": "keyboard-interactive,password",
}


OPENSSH_MAGIC = b"openssh-key-v1\x00"
MAX_KEY_FILE = 64 * 1024  # an OpenSSH private key is a few KiB even for RSA-16384


def _string(data: bytes, at: int) -> tuple[bytes, int]:
    if at + 4 > len(data):
        raise ValueError("short")
    size = int.from_bytes(data[at : at + 4], "big")
    if at + 4 + size > len(data):
        raise ValueError("short")
    return data[at + 4 : at + 4 + size], at + 4 + size


def private_key_public_blob(path: Path) -> bytes:
    """The public key blob stored in an OpenSSH private key file's unencrypted header. It is
    the key ssh will actually use, read without any passphrase, so an encrypted owner key is
    checked too and no secret is ever prompted for or read. Other formats are refused."""
    import base64

    if path.stat().st_size > MAX_KEY_FILE:
        raise PeerNetError("login-key", "not a private key file")
    text = path.read_text(encoding="ascii", errors="strict")
    lines = [line.strip() for line in text.strip().splitlines()]
    if (
        not lines
        or lines[0] != "-----BEGIN OPENSSH PRIVATE KEY-----"
        or lines[-1] != "-----END OPENSSH PRIVATE KEY-----"
    ):
        raise PeerNetError("login-key", "only OpenSSH-format private keys are supported")
    try:
        data = base64.b64decode("".join(lines[1:-1]), validate=True)
        if not data.startswith(OPENSSH_MAGIC):
            raise ValueError("magic")
        at = len(OPENSSH_MAGIC)
        for _ in range(3):  # cipher name, kdf name, kdf options
            _, at = _string(data, at)
        if data[at : at + 4] != (1).to_bytes(4, "big"):
            raise ValueError("one key")
        blob, _ = _string(data, at + 4)
    except ValueError:
        raise PeerNetError("login-key", "unreadable private key") from None
    return blob


def login_key_fingerprint(path: Path) -> str:
    """The fingerprint of the key in the private key file itself. A `.pub` beside it must
    agree, or the key is refused: a sidecar never stands in for the key ssh will use."""
    authorized.refuse_links(path)
    if not path.is_file():
        raise PeerNetError("login-key", "not a file")
    try:
        fp = authorized.fingerprint(private_key_public_blob(path))
    except OSError, UnicodeDecodeError:
        raise PeerNetError("login-key", "unreadable private key") from None
    pub = path.with_name(path.name + ".pub")
    if pub.exists():
        authorized.refuse_links(pub)
        import base64

        parts = pub.read_text(encoding="utf-8").split()
        try:
            sidecar = (
                authorized.fingerprint(base64.b64decode(parts[1], validate=True)) if len(parts) >= 2 else ""
            )
        except ValueError:
            sidecar = ""
        if sidecar != fp:
            raise PeerNetError("login-key", "its .pub does not match the private key")
    return fp


def refuse_peer_key(root: Path, conn: sqlite3.Connection, path: Path) -> None:
    """A login key must be the owner's own: never an `agent_peer_*` key, this device's
    enrolled key, or a key any device is granted with here."""
    fp = login_key_fingerprint(path)
    peer_fps: set[str] = set()
    for candidate in sorted((Path.home() / ".ssh").glob("agent_peer_*.pub")):
        try:
            peer_fps.add(
                authorized.fingerprint(authorized.ed25519_blob(candidate.read_text(encoding="utf-8")))
            )
        except OSError, PeerNetError:
            continue
    rows = conn.execute("SELECT key_fp FROM inbound").fetchall()
    own = conn.execute("SELECT v FROM enrollment WHERE k='key_fp'").fetchone()
    peer_fps |= {r[0] for r in rows} | ({own[0]} if own else set())
    if fp in peer_fps or path.name.startswith("agent_peer_"):
        raise PeerNetError("login-key", "a peer or enrolled key cannot authenticate the bootstrap")


def argv(
    root: Path,
    *,
    alias: str,
    user: str,
    host: str,
    port: int,
    remote_command: str,
    login_key: Path | None = None,
) -> list[str]:
    """The fixed bootstrap argv: B's pinned host key only, the owner's SSH config ignored,
    every forwarding off, and exactly one remote command."""
    authorized.refuse_links(ssh.pin_path(root, alias))
    auth = dict(PASSWORD_ONLY)
    identity: list[str] = []
    if login_key is not None:
        auth = {"PubkeyAuthentication": "yes", "IdentityAgent": "none", "IdentitiesOnly": "yes"}
        identity = ["-i", str(login_key)]
    opts = {
        **auth,
        "StrictHostKeyChecking": "yes",
        "UserKnownHostsFile": ssh.quoted(ssh.pin_path(root, alias)),
        "GlobalKnownHostsFile": ssh.null_device(),
        "HostKeyAlias": f"agent-peer-{alias}",
        "HostKeyAlgorithms": "ssh-ed25519",
        "UpdateHostKeys": "no",
        "CheckHostIP": "no",
        "ProxyCommand": "none",
        "ProxyJump": "none",
        "ControlMaster": "no",
        "ControlPath": "none",
        "ForwardAgent": "no",
        "ForwardX11": "no",
        "ClearAllForwardings": "yes",
        "RequestTTY": "no",
        "ConnectTimeout": "10",
        "ServerAliveInterval": "10",
        "ServerAliveCountMax": "3",
    }
    out = [ssh.binary("ssh"), "-F", str(ssh.empty_config(root)), *identity]
    out += ["-l", ssh.valid_user(user), "-p", str(ssh.valid_port(port))]
    for key, value in opts.items():
        out += ["-o", f"{key}={value}"]
    if "\n" in remote_command or "\r" in remote_command:
        raise PeerNetError("unsafe-path", "remote command")
    return [*out, "--", ssh.valid_host(host), remote_command]


class SshSession(LineChannel):
    """A `LineChannel` over the bootstrap ssh process, killed at `deadline_s` from spawn and on
    close if it has not exited by then."""

    def __init__(self, command: list[str], deadline_s: float) -> None:
        # stderr is inherited: ssh's prompts and B's refusals reach the owner, unsaved.
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.watchdog = threading.Timer(deadline_s, self._kill)
        self.watchdog.daemon = True
        self.watchdog.start()
        super().__init__(self.proc.stdout, self.proc.stdin)  # type: ignore[arg-type]

    def _kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()

    def close(self) -> None:
        super().close()
        self.watchdog.cancel()
        try:
            self.proc.wait(EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode


def opener(command: list[str], deadline_s: float):
    """The `session` factory `pairing.pair` calls."""
    return lambda: SshSession(command, deadline_s)
