"""Finding the Obsidian app on Windows. The owner's install was at
%LOCALAPPDATA%\\Obsidian, which the first folder guesses missed, so setup said the app was
missing when it was not."""

import subprocess
from pathlib import Path

import pytest

from agent_cli import installs


@pytest.fixture
def windows_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setattr(installs, "_protocol_command", lambda: "")
    return local


def _app(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return path


def test_the_per_user_install_folder_is_found(windows_home: Path):
    app = _app(windows_home / "Obsidian" / "Obsidian.exe")
    assert installs.obsidian_app_found(installs.WINDOWS) == str(app)


def test_the_programs_folder_is_still_found(windows_home: Path):
    app = _app(windows_home / "Programs" / "Obsidian" / "Obsidian.exe")
    assert installs.obsidian_app_found(installs.WINDOWS) == str(app)


def test_the_protocol_handler_finds_an_install_anywhere(
    windows_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = _app(tmp_path / "Apps" / "Obsidian Portable" / "Obsidian.exe")
    monkeypatch.setattr(installs, "_protocol_command", lambda: f'"{app}" "%1"')
    assert installs.obsidian_app_found(installs.WINDOWS) == str(app)


def test_a_stale_protocol_handler_falls_back_to_the_folders(
    windows_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(installs, "_protocol_command", lambda: r'"C:\gone\Obsidian.exe" "%1"')
    app = _app(windows_home / "Obsidian" / "Obsidian.exe")
    assert installs.obsidian_app_found(installs.WINDOWS) == str(app)


def test_nothing_installed_is_empty(windows_home: Path):
    assert installs.obsidian_app_found(installs.WINDOWS) == ""


@pytest.mark.skipif(installs.sys.platform != "win32", reason="reads the real Windows registry")
def test_the_registry_read_never_raises():
    assert isinstance(installs._protocol_command(), str)


@pytest.mark.parametrize(
    ("command", "exe"),
    [
        (
            r'"C:\Users\me\AppData\Local\Obsidian\Obsidian.exe" "%1"',
            r"C:\Users\me\AppData\Local\Obsidian\Obsidian.exe",
        ),
        (r"C:\Obsidian\Obsidian.exe %1", r"C:\Obsidian\Obsidian.exe"),
        ('"something-else" "%1"', None),
        ("", None),
    ],
)
def test_the_executable_is_read_from_the_open_command(monkeypatch: pytest.MonkeyPatch, command: str, exe):
    monkeypatch.setattr(installs, "_protocol_command", lambda: command)
    assert installs._obsidian_protocol_exe() == (Path(exe) if exe else None)


NO_NEWER = "No newer package versions are available"


@pytest.mark.parametrize(
    ("installer_rc", "on_path", "version_rc", "state", "detail"),
    [
        # winget's answer for a package already installed
        (1, True, 0, "installed", "already installed: C:/jq.exe"),
        (1, False, 0, "failed", NO_NEWER),
        (1, True, 1, "failed", NO_NEWER),
        # an installer that exits 0 but leaves nothing runnable
        (0, False, 0, "failed", "exited 0, but `jq --version` does not run"),
        (0, True, 1, "failed", "exited 0, but `jq --version` does not run"),
        (0, True, 0, "installed", "winget install"),
    ],
)
def test_install_success_is_a_working_tool_not_the_installer_exit(
    monkeypatch: pytest.MonkeyPatch,
    installer_rc: int,
    on_path: bool,
    version_rc: int,
    state: str,
    detail: str,
):
    def run(argv, *args, **kwargs):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, version_rc, "jq-1.7", "")
        return subprocess.CompletedProcess(argv, installer_rc, NO_NEWER if installer_rc else "", "")

    monkeypatch.setattr(installs.subprocess, "run", run)
    found = {"winget": "C:/winget.exe", "jq": "C:/jq.exe" if on_path else None}
    monkeypatch.setattr(installs.shutil, "which", lambda name: found.get(name))
    jq = next(t for t in installs.TOOLS if t.name == "jq")
    row = installs.install_missing([jq], installs.WINDOWS)[0]
    assert row["state"] == state and detail in row["detail"]


def test_setup_checks_reload_path_before_looking_for_tools(monkeypatch: pytest.MonkeyPatch):
    from agent_cli import setup

    order: list[str] = []
    monkeypatch.setattr(installs, "refresh_windows_path", lambda: order.append("refresh"))
    monkeypatch.setattr(setup.shutil, "which", lambda name: order.append(f"which {name}"))
    monkeypatch.setattr(installs, "obsidian_app_found", lambda system: "")
    monkeypatch.setattr(setup, "python_row", lambda: {"name": "python", "ok": True})
    setup.checks(installs.WINDOWS)
    assert order[0] == "refresh" and order[1].startswith("which ")
