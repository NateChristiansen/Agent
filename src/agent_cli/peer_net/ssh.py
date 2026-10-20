"""The one SSH call a sending device makes, and host-key pinning.

The argv is fixed: no shell, no remote command, the owner's own SSH config ignored (`-F` an
empty file also skips the system-wide one, per ssh(1)), every forwarding and sharing feature
off, and exactly one host key, the one the owner confirmed, accepted.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from pathlib import Path

from ..core import agent_path
from .authorized import fingerprint
from .errors import PeerNetError

USER_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
PEER_NET_REL = agent_path(Path(), "peer-net")
CALL_TIMEOUT_S = 30.0


def valid_user(user: str) -> str:
    if not USER_RE.fullmatch(user):
        raise PeerNetError("bad-value", "user")
    return user


def valid_host(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    labels = host.split(".")
    if host.startswith("-") or len(host) > 253 or not all(LABEL_RE.fullmatch(lab) for lab in labels):
        raise PeerNetError("bad-value", "host")
    return host


def valid_port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise PeerNetError("bad-value", "port")
    return port


def key_path(local_alias: str) -> Path:
    return Path.home() / ".ssh" / f"agent_peer_{local_alias}"


def pin_path(root: Path, alias: str) -> Path:
    return root / PEER_NET_REL / "known_hosts" / alias


def empty_config(root: Path) -> Path:
    """A zero-byte config file the CLI owns. Checked before every use: a file that grew would
    bring back the options `-F` exists to exclude."""
    from . import store

    path = root / PEER_NET_REL / "ssh_config"
    store.prepare_private(path.parent)  # never a world-readable peer-net directory
    if not path.exists():
        path.write_bytes(b"")
    if path.is_symlink() or not path.is_file() or path.stat().st_size != 0:
        raise PeerNetError("unsafe-path", "ssh_config")
    return path


def quoted(path: Path) -> str:
    text = str(path)
    if '"' in text or "\n" in text:
        raise PeerNetError("unsafe-path", "known_hosts")
    return f'"{text}"'


def binary(name: str) -> str:
    """Use Windows inbox OpenSSH from SystemRoot/System32/OpenSSH; PATH may select Git's different
    config and algorithm behavior. Other platforms use their configured SSH executable.
    """
    if os.name != "nt":
        return name
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / f"{name}.exe"
    if not path.is_file():
        raise PeerNetError("no-openssh", "install the Windows OpenSSH Client optional feature")
    return str(path)


def null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def argv(
    root: Path,
    *,
    local_alias: str,
    alias: str,
    user: str,
    host: str,
    port: int,
    known_hosts: Path | None = None,
) -> list[str]:
    """`known_hosts` replaces the live pin with a verified per-attempt snapshot of it, so a pin
    replaced concurrently can never redirect a saved envelope."""
    from .authorized import refuse_links

    trusted = known_hosts or pin_path(root, alias)
    refuse_links(trusted)  # a linked pin could swap the trusted host key
    opts = {
        "IdentitiesOnly": "yes",
        "IdentityAgent": "none",
        "BatchMode": "yes",
        "StrictHostKeyChecking": "yes",
        # A list-valued option: ssh splits it on whitespace, so the path is quoted inside the
        # value even though argv itself needs no shell quoting.
        "UserKnownHostsFile": quoted(trusted),
        "GlobalKnownHostsFile": null_device(),
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
        "ConnectTimeout": "5",
        "ServerAliveInterval": "5",
        "ServerAliveCountMax": "2",
    }
    out = [binary("ssh"), "-F", str(empty_config(root)), "-i", str(key_path(local_alias))]
    out += ["-l", valid_user(user), "-p", str(valid_port(port))]
    for key, value in opts.items():
        out += ["-o", f"{key}={value}"]
    return [*out, valid_host(host)]


def run(command: list[str], stdin: bytes, timeout: float = CALL_TIMEOUT_S) -> tuple[int, bytes, bytes]:
    """Run with a hard deadline; on expiry the child is killed and collected on every OS."""
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise PeerNetError("timeout", "ssh") from None
    return proc.returncode, out, err


ERR_LIMIT = 8 * 1024
CLEANUP_S = 0.5  # reserved inside run_bounded's timeout for kill, reap and joins


def run_bounded(
    command: list[str], stdin: bytes, *, timeout: float, limit: int
) -> tuple[int | None, bytes, bool]:
    """(exit status, stdout, overflowed), with every bound enforced while it runs: at most
    `limit` bytes of stdout are kept, and one more byte kills the process; stderr is drained
    and discarded. The exit status is None when the process had to be killed.

    The whole call returns within `timeout`: the process gets `timeout - CLEANUP_S`, and the
    kill, reap and reader joins get the reserved CLEANUP_S, each wait bounded by what is left.
    A process that survives SIGKILL past that allowance is left to the OS rather than waited
    on; that is the one case where reaping happens after return."""
    import threading
    import time

    end = time.monotonic() + max(0.0, timeout)
    deadline = end - CLEANUP_S
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    out = bytearray()
    overflow: list[bool] = []

    def pump(fd: int, buf: bytearray | None, cap: int) -> None:
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    return
                if buf is None:
                    continue  # stderr: drained so ssh never blocks on it, never kept
                buf.extend(chunk[: cap + 1 - len(buf)])
                if len(buf) > cap:
                    overflow.append(True)
                    proc.kill()
                    return
        except OSError:
            return

    def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(stdin)
            proc.stdin.close()
        except OSError:
            pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout.fileno(), out, limit), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr.fileno(), None, ERR_LIMIT), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for thread in threads:
        thread.start()
    killed = False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        proc.kill()
        killed = True
        try:
            proc.wait(timeout=max(0.0, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass  # survived SIGKILL past the allowance; left to the OS
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    status = None if killed or overflow else proc.returncode
    return status, bytes(out[:limit]), bool(overflow)


SCAN_ALIAS = "agent-peer-scan"


def scan_argv(config: Path, known: Path, host: str, port: int) -> list[str]:
    """Probe the server's ed25519 key with ssh into an empty temporary known_hosts file and every
    authentication method disabled. This cannot log in and negotiates the same algorithms as
    bootstrap and sends; Windows ssh-keyscan 9.5p2 can reject OpenSSH 10.3 key exchange.
    """
    opts = {
        "BatchMode": "yes",
        "StrictHostKeyChecking": "accept-new",
        "UserKnownHostsFile": quoted(known),
        "GlobalKnownHostsFile": null_device(),
        "HostKeyAlgorithms": "ssh-ed25519",
        "HostKeyAlias": SCAN_ALIAS,
        "UpdateHostKeys": "no",
        "CheckHostIP": "no",
        "PubkeyAuthentication": "no",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "GSSAPIAuthentication": "no",
        "HostbasedAuthentication": "no",
        "IdentityAgent": "none",
        "IdentitiesOnly": "yes",
        "ProxyCommand": "none",
        "ProxyJump": "none",
        "ControlMaster": "no",
        "ControlPath": "none",
        "ForwardAgent": "no",
        "ClearAllForwardings": "yes",
        "RequestTTY": "no",
        "ConnectTimeout": "5",
    }
    out = [binary("ssh"), "-F", str(config), "-l", SCAN_ALIAS, "-p", str(valid_port(port))]
    for key, value in opts.items():
        out += ["-o", f"{key}={value}"]
    return [*out, "--", valid_host(host), "true"]


def scan(host: str, port: int) -> list[str]:
    """The server's ed25519 host-key lines, as `<type> <base64>`. ssh is expected to fail at
    authentication; only the key it recorded during key exchange is read."""
    import tempfile

    host, port = valid_host(host), valid_port(port)
    with tempfile.TemporaryDirectory(prefix="peer-net-scan-") as tmp:
        config, known = Path(tmp) / "config", Path(tmp) / "known_hosts"
        config.write_bytes(b"")
        _, _, err = run(scan_argv(config, known, host, port), b"", timeout=15)
        data = known.read_text(encoding="utf-8", errors="replace") if known.is_file() else ""
    lines: list[str] = []
    for line in data.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == SCAN_ALIAS and parts[1] == "ssh-ed25519":
            lines.append(f"{parts[1]} {parts[2]}")
    if not lines:
        reason = err.decode("utf-8", errors="replace").strip().splitlines()
        raise PeerNetError("unreachable", (reason[-1][:160] if reason else "no host key"))
    return lines


def pick(scanned: list[str], wanted_fp: str) -> str:
    """The one scanned key whose fingerprint the owner supplied; never any other."""
    import base64

    matches: list[str] = []
    for line in scanned:
        try:
            blob = base64.b64decode(line.split()[1], validate=True)
        except ValueError:
            continue
        if fingerprint(blob) == wanted_fp:
            matches.append(line)
    if len(matches) != 1:
        raise PeerNetError("fingerprint-mismatch", "host")
    return matches[0]


def pin_line(alias: str, key_line: str) -> str:
    return f"agent-peer-{alias} {key_line}\n"
