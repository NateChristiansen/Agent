"""Which authorized_keys file sshd reads for this account, on each OS.

Microsoft's OpenSSH reads `%ProgramData%\\ssh\\administrators_authorized_keys` for a member of
Administrators (S-1-5-32-544), whether or not the shell is elevated, and `~/.ssh/authorized_keys`
for everyone else; POSIX sshd reads `~/.ssh/authorized_keys`. The resolver is exercised with
the OS and group facts mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.peer_net import system

ADMIN_GROUPS = "BUILTIN\\Administrators  Alias  S-1-5-32-544  Group used for deny only\n"
USER_GROUPS = "BUILTIN\\Users  Alias  S-1-5-32-545  Mandatory group\n"


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
