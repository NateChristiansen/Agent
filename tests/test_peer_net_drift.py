"""Idempotent re-runs never accept drift as success (slice 1 review S1-S4).

A re-run returns "already" only when the durable state is exactly what enrollment wrote: the
same key material, the exact managed line, a pin that is a plain file holding the confirmed
key. Anything else is refused or reported, and a failed re-verification clears `verified`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_cli.peer_net import enroll, ssh, store, system
from agent_cli.peer_net.errors import PeerNetError
from agent_cli.peer_net.recover import recover
from test_peer_net_enroll import (  # noqa: F401 -- shared fixtures
    MAC_PUB,
    ROGUE,
    SCANNED_FP,
    _keys,
    _ready,
    home,
    remote,
)


def _code(call) -> str:
    with pytest.raises(PeerNetError) as caught:
        call()
    return caught.value.code


# S1: init's re-run checks the identity it reprints.


def test_init_rerun_refuses_a_replaced_pub(tmp_root, home):
    enroll.init(tmp_root, "windows")
    pub = home / ".ssh" / "agent_peer_windows.pub"
    pub.write_text(MAC_PUB + "\n")
    assert _code(lambda: enroll.init(tmp_root, "windows")) == "key-drift"


def test_init_rerun_refuses_a_replaced_private_key(tmp_root, home, tmp_path):
    enroll.init(tmp_root, "windows")
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    key = home / ".ssh" / "agent_peer_windows"
    key.write_bytes(other.read_bytes())
    assert _code(lambda: enroll.init(tmp_root, "windows")) == "key-drift"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_init_rerun_refuses_a_readable_private_key(tmp_root, home):
    enroll.init(tmp_root, "windows")
    os.chmod(home / ".ssh" / "agent_peer_windows", 0o644)
    assert _code(lambda: enroll.init(tmp_root, "windows")) == "key-drift"


# S2: authorize's no-op needs the exact managed line, and the ACL check runs first.


def _rewrite(home: Path, old: bytes, new: bytes) -> None:
    data = _keys(home).read_bytes()
    assert old in data
    _keys(home).write_bytes(data.replace(old, new))


def test_authorize_rerun_refuses_a_line_without_restrict(tmp_root, home):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    _rewrite(home, b"restrict,command=", b"command=")
    assert _code(lambda: enroll.authorize(tmp_root, "mac", MAC_PUB)) == "grant-drift"


def test_authorize_rerun_refuses_an_altered_command(tmp_root, home):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    _rewrite(home, b"--from-device mac", b"--from-device windows")
    before = _keys(home).read_bytes()
    assert _code(lambda: enroll.authorize(tmp_root, "mac", MAC_PUB)) == "grant-drift"
    assert _keys(home).read_bytes() == before  # refused, never silently rewritten


@pytest.mark.parametrize("extra", [b"port-forwarding,", b"agent-forwarding,", b"pty,", b"X11-forwarding,"])
def test_authorize_rerun_refuses_an_option_that_reenables_a_capability(tmp_root, home, extra):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    _rewrite(home, b"restrict,", b"restrict," + extra)
    before = _keys(home).read_bytes()
    assert _code(lambda: enroll.authorize(tmp_root, "mac", MAC_PUB)) == "grant-drift"
    assert _keys(home).read_bytes() == before


def test_authorize_rerun_refuses_a_second_unrestricted_copy(tmp_root, home):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    with _keys(home).open("a") as fh:
        fh.write(MAC_PUB.replace(" agent-peer-net:mac", " copy") + "\n")
    assert _code(lambda: enroll.authorize(tmp_root, "mac", MAC_PUB)) == "grant-drift"


def test_authorize_rerun_checks_the_admin_acl_before_any_noop(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    keys = _keys(home)
    monkeypatch.setattr(system, "authorized_file", lambda: (keys, True))
    monkeypatch.setattr(system, "is_elevated", lambda: True)
    monkeypatch.setattr(system, "acl_sids", lambda path: {system.ADMINS_SID, system.SYSTEM_SID, "S-1-5-11"})
    assert _code(lambda: enroll.authorize(tmp_root, "mac", MAC_PUB)) == "acl"


# S3: a failed re-verification clears verified, whatever the reason.


def _verified(root: Path) -> bool:
    return enroll.show(root)["outbound"][0]["verified"]


def test_add_rerun_clears_verified_when_the_scan_fails(tmp_root, home, remote, monkeypatch):
    _ready(tmp_root)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    assert _verified(tmp_root) is True

    def unreachable(host, port):
        raise PeerNetError("unreachable", "host")

    monkeypatch.setattr(ssh, "scan", unreachable)
    assert _code(lambda: enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)) == "unreachable"
    assert _verified(tmp_root) is False and ssh.pin_path(tmp_root, "mac").exists()


def test_add_rerun_clears_verified_when_the_host_key_changed(tmp_root, home, remote, monkeypatch):
    _ready(tmp_root)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    monkeypatch.setattr(ssh, "scan", lambda host, port: [ROGUE])
    assert _code(lambda: enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)) == (
        "fingerprint-mismatch"
    )
    assert _verified(tmp_root) is False


def test_add_rerun_clears_verified_when_the_pin_cannot_be_read(tmp_root, home, remote, monkeypatch):
    _ready(tmp_root)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    pin = ssh.pin_path(tmp_root, "mac")
    real = Path.read_bytes

    def denied(self):
        if self == pin:
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert (
        _code(lambda: enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)) == "reverify-failed"
    )
    monkeypatch.undo()
    assert _verified(tmp_root) is False and pin.exists()


def test_add_rerun_clears_verified_when_ssh_cannot_start(tmp_root, home, remote, monkeypatch):
    _ready(tmp_root)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)

    def no_ssh(command, stdin, timeout=30.0):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(ssh, "run", no_ssh)
    out = enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    assert out["verified"] is False and out["probe"]["error"] == "ssh-failed"
    assert _verified(tmp_root) is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_pin_is_refused_and_unverifies(tmp_root, home, remote, tmp_path):
    _ready(tmp_root)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    pin = ssh.pin_path(tmp_root, "mac")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(pin.read_bytes())
    pin.unlink()
    pin.symlink_to(elsewhere)
    assert _code(lambda: enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)) == "unsafe-path"
    assert _verified(tmp_root) is False
    with pytest.raises(PeerNetError):
        ssh.argv(tmp_root, local_alias="windows", alias="mac", user="user", host="h", port=22)


# S4: recovery actions run the same writer checks; reports stay read-only.


def _open_authorize_intent(tmp_root: Path, monkeypatch) -> str:
    _ready(tmp_root)
    real = enroll.authorized.rewrite_in_place

    def die(*a, **k):
        real(*a, **k)
        raise OSError("killed after the write")

    monkeypatch.setattr(enroll.authorized, "rewrite_in_place", die)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.undo()
    [report] = recover(tmp_root)["open"]
    return report["op_id"]


def test_recovery_actions_refuse_another_account_but_reports_work(tmp_root, home, monkeypatch):
    op = _open_authorize_intent(tmp_root, monkeypatch)
    monkeypatch.setattr(system, "account_id", lambda: "uid:99999")
    assert recover(tmp_root)["open"][0]["status"] == "written"  # a report needs no identity
    assert _code(lambda: recover(tmp_root, op, "complete")) == "wrong-account"


@pytest.mark.skipif(os.name == "nt", reason="POSIX euid")
def test_recovery_actions_refuse_root(tmp_root, home, monkeypatch):
    op = _open_authorize_intent(tmp_root, monkeypatch)
    monkeypatch.setattr(system.os, "geteuid", lambda: 0)
    assert _code(lambda: recover(tmp_root, op, "complete")) == "root"


def test_a_report_on_an_old_store_does_not_adopt(tmp_root, home, monkeypatch):
    _open_authorize_intent(tmp_root, monkeypatch)
    conn = store.connect(tmp_root)
    with store.transaction(conn):
        conn.execute("DELETE FROM enrollment WHERE k='account'")
    conn.close()
    recover(tmp_root)
    conn = store.connect(tmp_root)
    assert conn.execute("SELECT v FROM enrollment WHERE k='account'").fetchone() is None
    conn.close()
