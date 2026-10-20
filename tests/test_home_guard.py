"""The suite's Windows home isolation (conftest.py, 059): `~` follows $HOME on every platform."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import posix_expanduser


def test_tilde_follows_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert posix_expanduser("~") == str(tmp_path)
    assert posix_expanduser("~/.ssh/agent_peer_x") == str(tmp_path) + "/.ssh/agent_peer_x"
    assert posix_expanduser("~\\.ssh") == str(tmp_path) + "\\.ssh"
    assert posix_expanduser("plain/path") == "plain/path"


def test_path_home_follows_a_home_set_inside_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The peer_net fixtures and bootstrap tests set HOME after the autouse patch is in place.
    home = tmp_path / "late-home"
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    assert Path("~/.ssh").expanduser() == home / ".ssh"


@pytest.mark.skipif(sys.platform != "win32", reason="USERPROFILE only decides home on Windows")
def test_windows_ignores_the_real_profile_while_home_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "real-profile"
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert Path.home() == tmp_path / "home"
