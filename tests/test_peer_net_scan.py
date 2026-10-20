"""Reading a device's host key, and which OpenSSH runs.

The scan is `ssh` itself with every authentication method off, since Windows' ssh-keyscan
9.5p2 failed key exchange against the Mac's OpenSSH 10.3 on 2026-10-04 while its ssh.exe
negotiated fine."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli.peer_net import ssh
from agent_cli.peer_net.errors import PeerNetError


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")
def test_the_host_key_scan_can_never_log_in(tmp_path):
    config = tmp_path / "config"
    config.write_bytes(b"")
    command = ssh.scan_argv(config, tmp_path / "known", "192.0.2.9", 22)
    done = subprocess.run([ssh.binary("ssh"), "-G", *command[1:]], capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    parsed = set(done.stdout.lower().splitlines())
    for line in [
        "batchmode yes",
        "pubkeyauthentication false",
        "passwordauthentication no",
        "kbdinteractiveauthentication no",
        "gssapiauthentication no",
        "hostbasedauthentication no",
        "identityagent none",
        "stricthostkeychecking accept-new",
        "hostkeyalias agent-peer-scan",
        "forwardagent no",
        "clearallforwardings yes",
    ]:
        assert line in parsed, line


def _fake_ssh(monkeypatch, written: str, stderr: bytes = b"") -> None:
    def run(command, stdin, timeout=30.0):
        option = next(c for c in command if c.startswith("UserKnownHostsFile="))
        known = Path(option.split("=", 1)[1].strip('"'))
        if written:
            known.write_text(written)
        return 255, b"", stderr

    monkeypatch.setattr(ssh, "run", run)


def test_the_scan_reads_only_the_key_ssh_recorded_for_its_own_alias(monkeypatch):
    _fake_ssh(
        monkeypatch,
        "agent-peer-scan ssh-ed25519 AAAAkey\nother-host ssh-ed25519 AAAAother\nagent-peer-scan ssh-rsa AAAArsa\n",
        b"agent-peer-scan@192.0.2.9: Permission denied (publickey,password).\n",
    )
    assert ssh.scan("192.0.2.9", 22) == ["ssh-ed25519 AAAAkey"]  # the auth failure is expected


def test_a_failed_key_exchange_is_reported_with_ssh_s_reason(monkeypatch):
    _fake_ssh(monkeypatch, "", b"choose_kex: unsupported KEX method sntrup761x25519-sha512@openssh.com\n")
    with pytest.raises(PeerNetError) as caught:
        ssh.scan("192.0.2.9", 22)
    assert caught.value.code == "unreachable" and "choose_kex" in caught.value.detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX resolves OpenSSH on PATH")
def test_posix_runs_openssh_from_path():
    assert ssh.binary("ssh") == "ssh"


@pytest.mark.skipif(os.name != "nt", reason="Windows pins the inbox OpenSSH")
def test_windows_runs_only_the_inbox_openssh():
    assert ssh.binary("ssh").lower().endswith("\\system32\\openssh\\ssh.exe")
