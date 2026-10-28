"""Which authorized_keys file sshd reads for this account, on each OS.

Microsoft's OpenSSH reads `%ProgramData%\\ssh\\administrators_authorized_keys` for a member of
Administrators (S-1-5-32-544), whether or not the shell is elevated, and `~/.ssh/authorized_keys`
for everyone else; POSIX sshd reads `~/.ssh/authorized_keys`. The resolver is exercised with
the OS and group facts mocked.
"""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli.peer_net import system
from agent_cli.peer_net.errors import PeerNetError

ADMIN_GROUPS = "BUILTIN\\Administrators  Alias  S-1-5-32-544  Group used for deny only\n"
USER_GROUPS = "BUILTIN\\Users  Alias  S-1-5-32-545  Mandatory group\n"


@pytest.fixture(autouse=True)
def _isolated_sid_cache(monkeypatch):
    """Discard fake identities without clearing any previously cached real identity."""
    monkeypatch.setattr(system, "_user_sid", cache(system._user_sid.__wrapped__))


class _OsAs:
    """`os` as seen from another platform, for this module only: setting `os.name` globally
    would also switch pathlib's flavour and break every path."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str):
        import os

        return getattr(os, attr)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(system, "os", _OsAs("nt"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.setattr(system.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


def test_posix_reads_the_users_own_file(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "os", _OsAs("posix"))
    monkeypatch.setattr(system.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    path, admin = system.authorized_file()
    assert (path, admin) == (tmp_path / "home" / ".ssh" / "authorized_keys", False)


def test_a_windows_administrator_uses_the_shared_admin_file_even_unelevated(windows, monkeypatch):
    # "Group used for deny only" is how an unelevated token lists Administrators: still a member.
    monkeypatch.setattr(system, "_whoami", lambda *args: ADMIN_GROUPS)
    path, admin = system.authorized_file()
    assert admin is True
    assert path == windows / "ProgramData" / "ssh" / "administrators_authorized_keys"
    assert system.is_elevated() is False  # elevation is a separate question


def test_a_windows_standard_user_uses_their_own_file(windows, monkeypatch):
    monkeypatch.setattr(system, "_whoami", lambda *args: USER_GROUPS)
    path, admin = system.authorized_file()
    assert (path, admin) == (windows / "home" / ".ssh" / "authorized_keys", False)


def test_elevation_is_read_from_the_high_integrity_label(windows, monkeypatch):
    high = ADMIN_GROUPS + "Mandatory Label\\High Mandatory Level  Label  S-1-16-12288\n"
    monkeypatch.setattr(system, "_whoami", lambda *args: high)
    assert system.is_elevated() is True


@pytest.mark.parametrize("sid", ["S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002"])
def test_identity_and_acl_consumers_share_one_user_query(windows, monkeypatch, sid):
    calls = []
    acl_sids = {sid, system.SYSTEM_SID}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "whoami":
            if command[1] == "/user":
                assert command == ["whoami", "/user", "/fo", "csv", "/nh"]
                stdout = f'"host\\me","{sid}"\n'
            else:
                assert command == ["whoami", "/groups"]
                stdout = ADMIN_GROUPS
        elif command[0] == "powershell":
            stdout = f"OWNER {sid}\n" + "".join(f"SID {s}\n" for s in acl_sids) + "END\n"
        else:
            assert command[0] == "icacls"
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(system.subprocess, "run", run)
    path = windows / "state.db"
    assert system.account_id() == "sid:" + sid
    assert system.account_id() == "sid:" + sid
    assert system.user_sid() == sid
    system.restrict_to_user(path)
    system.restrict_to_user(windows)
    assert system.is_private(path) is True
    acl_sids.add("S-1-5-21-9-9-9-9999")
    assert system.is_private(path) is False
    assert system.is_admin_member() is True
    assert system.is_elevated() is False
    grants = [command for command, _ in calls if command[0] == "icacls"]
    assert grants == [
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
        ["icacls", str(windows), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
    ]
    user_queries = [command for command, _ in calls if "/user" in command]
    group_queries = [command for command, _ in calls if "/groups" in command]
    assert len(group_queries) == 2
    assert len(user_queries) == 1


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (OSError("unavailable"), PeerNetError),
        (subprocess.TimeoutExpired("whoami", 10), PeerNetError),
        (subprocess.CalledProcessError(1, "whoami"), PeerNetError),
        ("", IndexError),
    ],
)
def test_failed_sid_lookup_does_not_poison_later_success(monkeypatch, outcome, error):
    calls = []
    sid = "S-1-5-21-1-2-3-1009"

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            if isinstance(outcome, Exception):
                raise outcome
            return subprocess.CompletedProcess(command, 0, outcome, "")
        return subprocess.CompletedProcess(command, 0, f'"host\\me","{sid}"\n', "")

    monkeypatch.setattr(system.subprocess, "run", run)
    with pytest.raises(error) as caught:
        system.user_sid()
    if isinstance(caught.value, PeerNetError):
        assert (caught.value.code, caught.value.detail) == ("system", "whoami")
    assert system.user_sid() == sid
    assert system.user_sid() == sid
    assert len(calls) == 2


def test_sid_cache_preserves_cold_and_warm_deadlines(monkeypatch):
    calls = []
    sid = "S-1-5-21-1-2-3-1010"

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, f'"host\\me","{sid}"\n', "")

    monkeypatch.setattr(system.subprocess, "run", run)
    monkeypatch.setattr(system, "time", SimpleNamespace(monotonic=lambda: 100.0))
    with system.deadline(99.0), pytest.raises(PeerNetError) as cold:
        system.user_sid()
    assert (cold.value.code, cold.value.detail) == ("timeout", "permission check")
    assert calls == []
    with system.deadline(100.7):
        assert system.user_sid() == sid
    assert calls[0][1]["timeout"] == pytest.approx(0.7)
    with system.deadline(99.0), pytest.raises(PeerNetError) as warm:
        system.user_sid()
    assert (warm.value.code, warm.value.detail) == ("timeout", "permission check")
    assert system.user_sid() == sid
    assert len(calls) == 1
