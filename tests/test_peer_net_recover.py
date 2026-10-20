"""Recovery after torn writes, foreign files and permissive umasks."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from agent_cli.peer_net import authorized, enroll, ssh, store, system
from agent_cli.peer_net.errors import PeerNetError
from agent_cli.peer_net.recover import recover
from test_peer_net_enroll import MAC_PUB, _keys, _ready, _status_ok, home  # noqa: F401 -- shared fixture


def _torn(monkeypatch, *, after_calls: int = 0):
    """Make the next rewrite (after `after_calls` clean ones) write half its bytes and die."""
    real = authorized.rewrite_in_place
    seen = {"n": 0}

    def rewrite(path, expected, new):
        seen["n"] += 1
        if seen["n"] <= after_calls:
            return real(path, expected, new)
        with Path(path).open("r+b") as fh:
            fh.write(new[: len(new) // 2])
            fh.truncate()
        raise OSError("killed mid-write")

    monkeypatch.setattr(authorized, "rewrite_in_place", rewrite)
    return real


OWNER_KEYS = b"ssh-rsa AAAAB3Nza owner@laptop\nssh-ed25519 AAAAC3Nz other@phone\n"


def test_a_revoke_torn_mid_write_is_a_conflict_restored_from_its_backup(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    (home / ".ssh").mkdir(exist_ok=True)
    _keys(home).write_bytes(OWNER_KEYS)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    real = _torn(monkeypatch)
    with pytest.raises(OSError):
        enroll.revoke(tmp_root, "mac")
    monkeypatch.setattr(authorized, "rewrite_in_place", real)
    assert not _status_ok(tmp_root)  # denied before the write began
    [report] = recover(tmp_root)["open"]
    assert report["status"] == "conflict"
    recover(tmp_root, report["op_id"], "restore-backup", expect=report["current_sha"])
    [again] = recover(tmp_root)["open"]
    assert again["status"] == "not-written"
    recover(tmp_root, again["op_id"], "complete")
    assert _keys(home).read_bytes() == OWNER_KEYS and recover(tmp_root)["open"] == []


def test_a_recovery_write_torn_mid_way_is_recoverable_again(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    (home / ".ssh").mkdir(exist_ok=True)
    _keys(home).write_bytes(OWNER_KEYS)
    real = _torn(monkeypatch, after_calls=1)  # the authorize writes, then dies after
    original_update = enroll.intents.update

    def die_on_done(conn, op, *, state=None, **payload):
        if state == "done":
            raise OSError("killed before activation")
        return original_update(conn, op, state=state, **payload)

    monkeypatch.setattr(enroll.intents, "update", die_on_done)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.setattr(enroll.intents, "update", original_update)
    [report] = recover(tmp_root)["open"]
    assert report["status"] == "written"
    with pytest.raises(OSError):
        recover(tmp_root, report["op_id"], "undo")  # the undo's own write is torn
    monkeypatch.setattr(authorized, "rewrite_in_place", real)
    [conflict] = recover(tmp_root)["open"]
    assert conflict["status"] == "conflict"
    recover(tmp_root, conflict["op_id"], "restore-backup", expect=conflict["current_sha"])
    assert _keys(home).read_bytes() == OWNER_KEYS and not _status_ok(tmp_root)


def test_an_external_edit_that_keeps_the_managed_line_is_still_a_conflict(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    (home / ".ssh").mkdir(exist_ok=True)
    _keys(home).write_bytes(OWNER_KEYS)
    original_update = enroll.intents.update

    def die_on_done(conn, op, *, state=None, **payload):
        if state == "done":
            raise OSError("killed before activation")
        return original_update(conn, op, state=state, **payload)

    monkeypatch.setattr(enroll.intents, "update", die_on_done)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.undo()
    data = _keys(home).read_bytes()
    _keys(home).write_bytes(data.replace(b"other@phone\n", b""))  # someone dropped a key
    [report] = recover(tmp_root)["open"]
    assert report["status"] == "conflict" and report["options"] == ["restore-backup"]
    with pytest.raises(PeerNetError):
        recover(tmp_root, report["op_id"], "complete")


def _crash_after_promotion(monkeypatch):
    real = enroll._generate

    def generate(*a, **k):
        real(*a, **k)
        raise OSError("killed before enrollment")

    monkeypatch.setattr(enroll, "_generate", generate)


def test_a_private_key_replaced_after_a_crash_is_never_deleted_or_adopted(tmp_root, home, monkeypatch):
    _crash_after_promotion(monkeypatch)
    with pytest.raises(OSError):
        enroll.init(tmp_root, "windows")
    key = home / ".ssh" / "agent_peer_windows"
    key.unlink()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    os.replace(f"{key}.pub", home / "elsewhere.pub")  # keep only the private half at that path
    replaced = key.read_bytes()
    [report] = recover(tmp_root)["open"]
    assert report["status"] == "conflict" and report["foreign"] == ["key"] and report["options"] == []
    for action in ("undo", "complete"):
        with pytest.raises(PeerNetError):
            recover(tmp_root, report["op_id"], action)
    assert key.read_bytes() == replaced


def test_a_crash_during_keygen_leaves_only_a_recorded_temp_dir(tmp_root, home, monkeypatch):
    real_run = subprocess.run

    def keygen_dies(command, *a, **k):
        if command[0] == "ssh-keygen" and "-f" in command:
            Path(command[command.index("-f") + 1]).write_text("half a key")
            raise OSError("killed during keygen")
        return real_run(command, *a, **k)

    monkeypatch.setattr(enroll.subprocess, "run", keygen_dies)
    with pytest.raises(OSError):
        enroll.init(tmp_root, "windows")
    monkeypatch.undo()
    [report] = recover(tmp_root)["open"]
    assert report["status"] == "not-promoted"
    recover(tmp_root, report["op_id"], "undo")
    assert not list((home / ".ssh").glob("agent_peer*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_state_backups_and_pins_are_private_under_a_permissive_umask(tmp_root, home, monkeypatch):
    old = os.umask(0)
    try:
        _ready(tmp_root)
        (home / ".ssh").mkdir(exist_ok=True)
        _keys(home).write_bytes(OWNER_KEYS)
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    finally:
        os.umask(old)
    peer_net = tmp_root / ".agent" / "peer-net"
    assert stat.S_IMODE(peer_net.stat().st_mode) == 0o700
    for path in [peer_net / "state.db", *peer_net.glob("state.db-*"), *(peer_net / "backups").glob("*.bak")]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE((peer_net / "backups").stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_store_others_can_read_is_refused(tmp_root, home):
    _ready(tmp_root)
    os.chmod(tmp_root / ".agent" / "peer-net", 0o755)
    with pytest.raises(PeerNetError) as caught:
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    assert caught.value.code == "unsafe-permissions"


def _authorize_dies_before_activation(tmp_root, home, monkeypatch) -> dict:
    _ready(tmp_root)
    (home / ".ssh").mkdir(exist_ok=True)
    _keys(home).write_bytes(OWNER_KEYS)
    original_update = enroll.intents.update

    def die_on_done(conn, op, *, state=None, **payload):
        if state == "done":
            raise OSError("killed before activation")
        return original_update(conn, op, state=state, **payload)

    monkeypatch.setattr(enroll.intents, "update", die_on_done)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.setattr(enroll.intents, "update", original_update)
    [report] = recover(tmp_root)["open"]
    return report


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
@pytest.mark.parametrize("target", ["dir", "db"])
def test_recovery_refuses_to_write_through_an_unsafe_store(tmp_root, home, monkeypatch, target):
    report = _authorize_dies_before_activation(tmp_root, home, monkeypatch)
    before = _keys(home).read_bytes()
    peer_net = tmp_root / ".agent" / "peer-net"
    os.chmod(peer_net if target == "dir" else peer_net / "state.db", 0o755 if target == "dir" else 0o644)
    for action in ("complete", "undo"):
        with pytest.raises(PeerNetError) as caught:
            recover(tmp_root, report["op_id"], action)
        assert caught.value.code == "unsafe-permissions"
    assert _keys(home).read_bytes() == before
    assert recover(tmp_root)["open"][0]["status"] == "written"  # report-only still works


def test_a_short_os_write_still_writes_the_whole_backup(tmp_root, monkeypatch):
    from agent_cli.peer_net import mutate

    real_write = os.write
    monkeypatch.setattr(mutate.os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    path = mutate.backup(tmp_root, "op", OWNER_KEYS)
    assert path.read_bytes() == OWNER_KEYS


def test_private_writes_open_in_binary_mode_where_the_os_has_one(tmp_root, monkeypatch):
    """Windows' text-mode fds rewrite "\\n" as "\\r\\n"; the flag must be passed where it exists."""
    from agent_cli.peer_net import mutate

    seen: list[int] = []
    real_open = os.open
    native = getattr(os, "O_BINARY", None)  # Windows has it; keep it in the real call there
    flag = native if native is not None else 0x8000
    if native is None:
        monkeypatch.setattr(mutate.os, "O_BINARY", flag, raising=False)

    def spy(path, flags, mode=0o777):
        seen.append(flags)
        return real_open(path, flags if native is not None else flags & ~flag, mode)

    monkeypatch.setattr(mutate.os, "open", spy)
    data = b"lf\ncrlf\r\nlf again\n"
    mutate.write_private(tmp_root / "x.bin", data)
    assert seen and seen[0] & flag and (tmp_root / "x.bin").read_bytes() == data


def test_a_corrupted_backup_is_never_restored(tmp_root, home, monkeypatch):
    report = _authorize_dies_before_activation(tmp_root, home, monkeypatch)
    _keys(home).write_bytes(OWNER_KEYS + b"an owner edit\n")  # now a conflict
    [conflict] = recover(tmp_root)["open"]
    assert conflict["status"] == "conflict"
    from agent_cli.peer_net import intents

    conn = store.connect(tmp_root)
    intent = intents.get(conn, report["op_id"])
    conn.close()
    assert intent is not None
    backup = Path(intent.payload["backup"])
    backup.write_bytes(b"truncated")
    with pytest.raises(PeerNetError) as caught:
        recover(tmp_root, conflict["op_id"], "restore-backup", expect=conflict["current_sha"])
    assert caught.value.code == "backup-mismatch"
    assert _keys(home).read_bytes() == OWNER_KEYS + b"an owner edit\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_promoted_key_others_can_read_is_not_adopted(tmp_root, home, monkeypatch):
    _crash_after_promotion(monkeypatch)
    with pytest.raises(OSError):
        enroll.init(tmp_root, "windows")
    os.chmod(home / ".ssh" / "agent_peer_windows", 0o644)
    [report] = recover(tmp_root)["open"]
    # ssh-keygen -y itself refuses an unprotected private key, so the key reads as foreign and
    # complete is not even offered; the explicit privacy check behind it covers Windows ACLs.
    assert report["status"] == "conflict"
    with pytest.raises(PeerNetError) as caught:
        recover(tmp_root, report["op_id"], "complete")
    assert caught.value.code in {"unsafe-permissions", "action-unavailable"}
    assert enroll.show(tmp_root)["device"] is None


@pytest.mark.parametrize(
    ("code", "stdout"),
    [
        (255, '{"v": 1, "ok": true, "device": "mac", "enabled": true, "proto": 1}\n'),
        (0, 'banner\n{"v": 1, "ok": true, "device": "mac", "enabled": true, "proto": 1}\n'),
        (0, '{"v": true, "ok": true, "device": "mac", "enabled": true, "proto": 1}\n'),
        (0, '{"v": 1, "ok": true, "device": "mac", "enabled": true, "proto": 1.0}\n'),
        (0, '{"v": 1, "ok": true, "device": "mac", "proto": 1}\n'),
    ],
)
def test_only_an_exact_status_answer_verifies_a_route(tmp_root, monkeypatch, code, stdout):
    monkeypatch.setattr(ssh, "run", lambda command, stdin, timeout=30.0: (code, stdout.encode(), b""))
    answer = enroll.probe_status(tmp_root, device="windows", alias="mac", user="user", host="h", port=22)
    assert not enroll.verified_answer(answer, "mac")


def test_the_exact_answer_does_verify():
    answer = json.loads('{"v": 1, "ok": true, "device": "mac", "enabled": true, "proto": 1}')
    assert enroll.verified_answer(answer, "mac")


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client")
def test_a_pin_path_with_spaces_survives_real_ssh_option_parsing(tmp_path):
    root = tmp_path / "harness with space"
    (root / ".agent").mkdir(parents=True)
    command = ssh.argv(root, local_alias="windows", alias="mac", user="user", host="192.0.2.1", port=2222)
    out = subprocess.run(
        [command[0], "-G", *command[1:]], capture_output=True, text=True, check=True, timeout=30
    )
    settings = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition(" ")
        settings.setdefault(key, value)
    pin = ssh.pin_path(root, "mac")
    assert (str(pin) if os.name == "nt" else pin.as_posix()) in settings["userknownhostsfile"]
    assert settings["userknownhostsfile"].count("harness with space") == 1  # one path, not split
    assert settings["hostkeyalias"] == "agent-peer-mac" and settings["port"] == "2222"
    assert settings["identitiesonly"] == "yes" and settings["forwardagent"] == "no"
    assert settings["hostkeyalgorithms"] == "ssh-ed25519"


@pytest.mark.parametrize(
    ("sids", "owner", "private"),
    [
        ({"S-1-3-4", "S-1-5-18", "S-1-5-32-544", "S-1-5-21-me"}, "S-1-5-32-544", True),  # observed live
        ({"S-1-5-21-me"}, "S-1-5-21-me", True),
        ({"S-1-3-4", "S-1-5-21-me"}, "S-1-5-21-other", False),  # OWNER RIGHTS for a stranger
        ({"S-1-5-11", "S-1-5-21-me"}, "S-1-5-21-me", False),  # Authenticated Users
    ],
)
def test_windows_privacy_accepts_python_mkdir_acl_only_for_a_trusted_owner(monkeypatch, sids, owner, private):
    monkeypatch.setattr(system.os, "name", "nt")
    monkeypatch.setattr(system, "acl_facts", lambda path: (owner, sids))
    monkeypatch.setattr(system, "user_sid", lambda: "S-1-5-21-me")
    assert system.is_private(Path("x")) is private


def test_system_privacy_check_reads_modes(tmp_path):
    path = tmp_path / "f"
    path.write_text("")
    if os.name != "nt":
        os.chmod(path, 0o644)
        assert not system.is_private(path)
        os.chmod(path, 0o600)
        assert system.is_private(path)
    assert store.STORE_REL.name == "state.db"
