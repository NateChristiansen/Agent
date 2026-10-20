"""Enrollment fails closed, keeps the owner's keys, and recovers only its own changes."""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from agent_cli.peer_net import authorized, enroll, launcher, receive, ssh, store, system
from agent_cli.peer_net.errors import PeerNetError
from agent_cli.peer_net.recover import recover
from peer_net_helpers import NOW, envelope, raw

NAME = b"ssh-ed25519"


def _pub(seed: int) -> str:
    blob = len(NAME).to_bytes(4, "big") + NAME + (32).to_bytes(4, "big") + bytes([seed] * 32)
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " agent-peer-net:mac"


MAC_PUB = _pub(7)


@pytest.fixture
def home(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows reads home from here, not HOME
    monkeypatch.setattr(system, "host_fingerprints", lambda: ["SHA256:hostkey"])
    keys = home / ".ssh" / "authorized_keys"
    monkeypatch.setattr(system, "authorized_file", lambda: (keys, False))
    monkeypatch.setattr(
        launcher,
        "resolve",
        lambda root: launcher.Launch("/usr/bin/python3", "/srv/agent/bin/peer-net-receive", "posix"),
    )
    return home


def _keys(home: Path) -> Path:
    return home / ".ssh" / "authorized_keys"


def test_init_refuses_before_any_write_when_sshd_has_no_host_keys(tmp_root, home, monkeypatch):
    monkeypatch.setattr(system, "host_fingerprints", lambda: [])
    with pytest.raises(PeerNetError) as caught:
        enroll.init(tmp_root, "windows")
    assert caught.value.code == "no-host-keys" and not store.exists(tmp_root)
    assert not (home / ".ssh").exists()


def test_an_unreadable_host_key_is_named_not_reported_missing(tmp_root, home, monkeypatch, tmp_path):
    monkeypatch.undo()  # the real host_fingerprints, against a key this process cannot read
    pub = tmp_path / "ssh_host_ed25519_key.pub"
    pub.write_text("ssh-ed25519 AAAA host\n")
    monkeypatch.setattr(system, "_ssh_dir", lambda: tmp_path)
    monkeypatch.setattr(Path, "read_text", lambda self, **k: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(PeerNetError) as caught:
        system.host_fingerprints()
    assert caught.value.code == "host-key-unreadable" and "elevated" in caught.value.detail


def _assert_user_only(path: Path, mode: int) -> None:
    """User-only: the exact POSIX mode, or on Windows an ACL that only the user's own accounts hold."""
    if os.name == "nt":
        assert system.is_private(path)
    else:
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_init_makes_a_user_only_key_and_leaves_the_device_disabled(tmp_root, home):
    out = enroll.init(tmp_root, "windows")
    key = home / ".ssh" / "agent_peer_windows"
    assert out["public_key"].startswith("ssh-ed25519 ") and out["host_fingerprints"] == ["SHA256:hostkey"]
    _assert_user_only(key, 0o600)
    assert enroll.show(tmp_root)["enabled"] is False
    assert enroll.init(tmp_root, "windows")["already"] is True  # idempotent, not an error


def test_init_never_overwrites_an_existing_key(tmp_root, home):
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "agent_peer_windows").write_text("someone else's key")
    with pytest.raises(PeerNetError) as caught:
        enroll.init(tmp_root, "windows")
    assert caught.value.code == "key-exists"
    assert (home / ".ssh" / "agent_peer_windows").read_text() == "someone else's key"


@pytest.mark.parametrize("fail_at", [1, 2])
def test_a_crash_between_key_promotions_is_undone_only_for_this_op(tmp_root, home, monkeypatch, fail_at):
    real_link = os.link
    calls = {"n": 0}

    def link(src, dst):
        calls["n"] += 1
        if calls["n"] == fail_at + 1:  # the crash lands after `fail_at` promotions
            raise OSError("killed")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link)
    if fail_at == 2:
        real_generate = enroll._generate

        def generate(*a, **k):
            real_generate(*a, **k)
            raise OSError("killed before enrollment")

        monkeypatch.setattr(enroll, "_generate", generate)
    with pytest.raises(OSError):
        enroll.init(tmp_root, "windows")
    [report] = recover(tmp_root)["open"]
    expected = "not-promoted" if fail_at == 1 else "keys-promoted"
    assert report["status"] == expected
    other = home / ".ssh" / "unrelated_key"
    other.write_text("keep")
    recover(tmp_root, report["op_id"], "undo")
    assert not (home / ".ssh" / "agent_peer_windows").exists() and other.read_text() == "keep"
    assert not list((home / ".ssh").glob("agent_peer.tmp-*"))


def test_a_crash_after_both_promotions_can_be_adopted(tmp_root, home, monkeypatch):
    real_generate = enroll._generate

    def generate(*a, **k):
        real_generate(*a, **k)
        raise OSError("killed before enrollment")

    monkeypatch.setattr(enroll, "_generate", generate)
    with pytest.raises(OSError):
        enroll.init(tmp_root, "windows")
    [report] = recover(tmp_root)["open"]
    recover(tmp_root, report["op_id"], "complete")
    assert enroll.show(tmp_root)["device"] == "windows" and recover(tmp_root)["open"] == []


def _ready(tmp_root: Path) -> None:
    enroll.init(tmp_root, "windows")
    enroll.set_enabled(tmp_root, True)


def _status_ok(tmp_root: Path) -> bool:
    common = {k: envelope()[k] for k in ("v", "id", "sent_at", "expires_at")}
    return receive(tmp_root, "mac", raw({**common, "verb": "status"}), clock=lambda: NOW).get("ok") is True


def test_authorize_appends_one_restricted_line_and_keeps_the_rest(tmp_root, home):
    _ready(tmp_root)
    _keys(home).write_bytes(b"ssh-rsa AAAAB3Nza owner@laptop")  # no final newline
    os.chmod(_keys(home), 0o600)
    out = enroll.authorize(tmp_root, "mac", MAC_PUB)
    data = _keys(home).read_bytes()
    assert data.startswith(b"ssh-rsa AAAAB3Nza owner@laptop\n")
    [line] = [x for x in data.decode().splitlines() if "agent-peer-net:mac:" in x]
    entry = authorized.parse_line(line)
    assert entry is not None and entry.options.startswith("restrict,")
    assert (
        authorized.option_command(entry)
        == '"/usr/bin/python3" -I "/srv/agent/bin/peer-net-receive" --from-device mac'
    )
    _assert_user_only(_keys(home), 0o600)
    assert out["key_fp"].startswith("SHA256:")
    assert _status_ok(tmp_root)


def test_authorize_refuses_a_key_already_present_in_any_form(tmp_root, home):
    _ready(tmp_root)
    (home / ".ssh").mkdir(exist_ok=True)
    _keys(home).write_text(MAC_PUB.replace(" agent-peer-net:mac", " owner-copy") + "\n")
    with pytest.raises(PeerNetError) as caught:
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    assert caught.value.code == "key-present" and enroll.show(tmp_root)["inbound"] == []


def test_authorize_creates_a_missing_file_user_only(tmp_root, home):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    _assert_user_only(_keys(home), 0o600)
    _assert_user_only(home / ".ssh", 0o700)


def _crash_after_write(monkeypatch):
    real = authorized.rewrite_in_place

    def rewrite(*a, **k):
        real(*a, **k)
        raise OSError("killed after the line was written")

    monkeypatch.setattr(authorized, "rewrite_in_place", rewrite)
    return real


def test_an_authorize_that_died_after_writing_stays_refused_until_recovered(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    real = _crash_after_write(monkeypatch)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.setattr(authorized, "rewrite_in_place", real)
    assert not _status_ok(tmp_root)  # pending: the receiver refuses
    with pytest.raises(PeerNetError):
        enroll.set_enabled(tmp_root, True)  # `on` waits for recovery
    enroll.set_enabled(tmp_root, False)  # `off` never does
    enroll.set_enabled(tmp_root, False)
    [report] = recover(tmp_root)["open"]
    assert report["status"] == "written"
    recover(tmp_root, report["op_id"], "complete")
    enroll.set_enabled(tmp_root, True)
    assert _status_ok(tmp_root)


def test_a_later_revoke_supersedes_an_interrupted_authorize(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    real = _crash_after_write(monkeypatch)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.setattr(authorized, "rewrite_in_place", real)
    enroll.revoke(tmp_root, "mac")
    report = next(r for r in recover(tmp_root)["open"] if r["op"] == "authorize")
    assert report["superseded"] is True and report["options"] == ["undo"]
    assert report["status"] == "not-written"  # the revoke already took the line out
    with pytest.raises(PeerNetError):
        recover(tmp_root, report["op_id"], "complete")
    assert "agent-peer-net:mac" not in _keys(home).read_text()  # revoke already removed it
    recover(tmp_root, report["op_id"], "undo")
    assert not _status_ok(tmp_root)


def test_a_conflicting_external_edit_is_restored_only_with_the_current_hash(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    (home / ".ssh").mkdir(exist_ok=True)
    _keys(home).write_text("original\n")

    def die_before_writing(*a, **k):
        raise OSError("killed before the write")

    monkeypatch.setattr(authorized, "rewrite_in_place", die_before_writing)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.undo()
    _keys(home).write_text("original\nthe owner added this\n")  # someone else's edit
    [report] = recover(tmp_root)["open"]
    assert report["status"] == "conflict" and "the owner added this" in report["diff"]
    with pytest.raises(PeerNetError):
        recover(tmp_root, report["op_id"], "restore-backup", expect="0" * 64)
    assert "the owner added this" in _keys(home).read_text()
    recover(tmp_root, report["op_id"], "restore-backup", expect=report["current_sha"])
    assert _keys(home).read_text() == "original\n"


def test_revoke_removes_only_its_line_and_allows_reenrollment(tmp_root, home):
    _ready(tmp_root)
    (home / ".ssh").mkdir(exist_ok=True)
    _keys(home).write_text("ssh-rsa AAAAB3Nza owner@laptop\n")
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    assert enroll.revoke(tmp_root, "mac")["lines_removed"] == 1
    assert _keys(home).read_text() == "ssh-rsa AAAAB3Nza owner@laptop\n" and not _status_ok(tmp_root)
    enroll.authorize(tmp_root, "mac", _pub(9))
    assert _status_ok(tmp_root)


SCANNED = (
    "ssh-ed25519 "
    + base64.b64encode(
        len(NAME).to_bytes(4, "big") + NAME + (32).to_bytes(4, "big") + bytes([3] * 32)
    ).decode()
)
SCANNED_FP = authorized.fingerprint(base64.b64decode(SCANNED.split()[1]))
ROGUE = (
    "ssh-ed25519 "
    + base64.b64encode(
        len(NAME).to_bytes(4, "big") + NAME + (32).to_bytes(4, "big") + bytes([4] * 32)
    ).decode()
)


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch):
    answer = {"v": 1, "ok": True, "device": "mac", "enabled": True, "proto": 1}
    monkeypatch.setattr(ssh, "scan", lambda host, port: [ROGUE, SCANNED])
    calls: list[list[str]] = []

    def run(command, stdin, timeout=30.0):
        calls.append(command)
        return 0, (json.dumps(answer) + "\n").encode(), b""

    monkeypatch.setattr(ssh, "run", run)
    return answer, calls


def test_add_pins_only_the_confirmed_key_and_verifies_the_route(tmp_root, home, remote):
    _ready(tmp_root)
    answer, calls = remote
    out = enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    pin = ssh.pin_path(tmp_root, "mac").read_text()
    assert out["verified"] is True and pin == f"agent-peer-mac {SCANNED}\n" and ROGUE not in pin
    command = calls[0]
    assert command[:3] == [ssh.binary("ssh"), "-F", str(ssh.empty_config(tmp_root))]
    assert "HostKeyAlgorithms=ssh-ed25519" in command and "IdentityAgent=none" in command
    assert command[-1] == "192.0.2.10"
    with pytest.raises(PeerNetError) as caught:
        enroll.add(tmp_root, "mac", "user", "192.0.2.10", 2222, SCANNED_FP)  # a differing field
    assert caught.value.code == "route-exists"


def test_a_wrong_device_or_fingerprint_never_verifies(tmp_root, home, remote):
    _ready(tmp_root)
    answer, _ = remote
    with pytest.raises(PeerNetError) as caught:
        enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, "SHA256:not-it")
    assert caught.value.code == "fingerprint-mismatch" and not ssh.pin_path(tmp_root, "mac").exists()
    answer["device"] = "laptop"
    assert enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)["verified"] is False
    assert enroll.show(tmp_root)["outbound"][0]["verified"] is False


def test_revoke_removes_the_route_and_pin_so_add_can_run_again(tmp_root, home, remote):
    _ready(tmp_root)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    enroll.revoke(tmp_root, "mac")
    assert enroll.show(tmp_root)["outbound"] == [] and not ssh.pin_path(tmp_root, "mac").exists()
    assert enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)["verified"] is True


@pytest.mark.parametrize(
    ("user", "host", "port"),
    [
        ("-oProxyCommand=x", "h", 22),
        ("user", "-oProxy", 22),
        ("user", "a..b", 22),
        ("user", "h", 0),
        ("na te", "h", 22),
    ],
)
def test_ssh_inputs_are_validated(tmp_root, user, host, port):
    with pytest.raises(PeerNetError):
        ssh.argv(tmp_root, local_alias="windows", alias="mac", user=user, host=host, port=port)


def test_ipv6_and_hostnames_pass_validation():
    assert ssh.valid_host("fe80::1") == "fe80::1" and ssh.valid_host("win-box.local") == "win-box.local"


def test_the_empty_ssh_config_must_stay_empty(tmp_root):
    path = ssh.empty_config(tmp_root)
    path.write_text("ProxyCommand evil\n")
    with pytest.raises(PeerNetError):
        ssh.empty_config(tmp_root)
