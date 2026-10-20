"""Finding and proving the Obsidian CLI.

Setup used to call the CLI optional and only looked for it on PATH, while the tab relabel used
a second resolver. It is now required, found by one resolver, and proved with the read-only
`obsidian version` when the app is running.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import obsidian


@pytest.fixture(autouse=True)
def _no_machine_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSIDIAN_CLI", raising=False)
    monkeypatch.setattr(obsidian, "CLI_FALLBACKS", ())
    monkeypatch.setattr(obsidian.shutil, "which", lambda name: None)


def test_env_override_must_be_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_CLI", str(tmp_path))  # a directory is not a CLI
    assert obsidian.cli_path() is None
    exe = tmp_path / "obsidian"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_CLI", str(exe))
    assert obsidian.cli_path() == str(exe)


def test_version_is_unverified_while_the_app_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obsidian, "app_running", lambda: False)
    monkeypatch.setattr(obsidian.subprocess, "run", lambda *a, **k: pytest.fail("probed a closed app"))
    assert obsidian.cli_version("/x/obsidian") == (None, "unverified: Obsidian is not running")


@pytest.mark.parametrize(
    ("result", "works", "detail"),
    [
        (
            subprocess.CompletedProcess([], 0, "1.13.7 (installer 1.13.7)\n", ""),
            True,
            "1.13.7 (installer 1.13.7)",
        ),
        (subprocess.CompletedProcess([], 1, "", "CLI is not enabled\n"), False, "CLI is not enabled"),
        (subprocess.CompletedProcess([], 0, "", ""), False, "exit 0"),
        (subprocess.TimeoutExpired(["obsidian", "version"], 10), False, "timed out"),
    ],
)
def test_version_with_the_app_open(
    monkeypatch: pytest.MonkeyPatch, result: object, works: bool, detail: str
) -> None:
    def run(argv, **kwargs):
        assert argv[1:] == ["version"] and kwargs["timeout"] == 10
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(obsidian, "app_running", lambda: True)
    monkeypatch.setattr(obsidian.subprocess, "run", run)
    got_works, got_detail = obsidian.cli_version("/x/obsidian")
    assert got_works is works and detail in got_detail


@pytest.mark.parametrize(
    ("version", "ok", "verified", "guide"),
    [
        ((True, "1.13.7"), True, True, ""),
        ((None, "unverified"), True, False, ""),
        ((False, "failed"), False, False, "g"),
    ],
)
def test_setup_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: tuple, ok: bool, verified: bool, guide: str
) -> None:
    exe = tmp_path / "obsidian"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_CLI", str(exe))
    monkeypatch.setattr(obsidian, "cli_version", lambda found: version)
    row = obsidian.cli_row("obsidian", "g")
    assert (row["ok"], row["verified"], row["guide"]) == (ok, verified, guide)
    assert row["found"].startswith(str(exe))


@pytest.mark.parametrize(
    ("comm", "running"),
    [
        ("/Applications/Obsidian.app/Contents/MacOS/Obsidian\n", True),
        ("/usr/lib/obsidian/obsidian\n", True),
        (
            "/Applications/Obsidian.app/Contents/Frameworks/Obsidian Helper.app/Contents/MacOS/Obsidian Helper\n",
            False,
        ),
        ("/bin/zsh\n", False),
    ],
)
def test_app_running_reads_ps_on_posix(monkeypatch: pytest.MonkeyPatch, comm: str, running: bool) -> None:
    """pgrep saw only the helper processes on the owner's Mac, never the main app."""
    monkeypatch.setattr(obsidian.sys, "platform", "darwin")
    monkeypatch.setattr(
        obsidian.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, comm, "")
    )
    assert obsidian.app_running() is running


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_a_hung_process_listing_reads_as_not_running(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    seen: list[float] = []

    def hang(argv, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(obsidian.sys, "platform", platform)
    monkeypatch.setattr(obsidian.subprocess, "run", hang)
    monkeypatch.setattr(obsidian.obsidian_run, "running", obsidian.app_running)  # no CLI socket here
    assert obsidian.app_running() is False
    assert seen == [obsidian.APP_QUERY_TIMEOUT_S]
    # so the version probe is skipped and reads unverified, never crashes setup
    assert obsidian.cli_version("/x/obsidian") == (None, "unverified: Obsidian is not running")


def test_vault_dir_skips_a_dangling_symlink(tmp_path: Path) -> None:
    link = tmp_path / "notes"
    try:
        link.symlink_to(tmp_path / "gone", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need a privilege this platform lacks")
    assert obsidian._vault_dir(link) is False
    assert obsidian._vault_dir(tmp_path / "journal") is True and (tmp_path / "journal").is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_vault_dir_skips_a_dangling_junction(tmp_path: Path) -> None:
    """Setup links notes folders as junctions on Windows, which is_symlink does not see."""
    import _winapi  # type: ignore[import-not-found]

    target = tmp_path / "notes-repo" / "notes"
    target.mkdir(parents=True)
    link = tmp_path / "notes"
    _winapi.CreateJunction(str(target), str(link))  # pyright: ignore[reportAttributeAccessIssue] — win32 only
    target.rmdir()
    assert obsidian._vault_dir(link) is False


def test_setup_row_when_absent() -> None:
    assert obsidian.cli_row("obsidian", "g") == {
        "name": "obsidian",
        "ok": False,
        "found": "",
        "guide": "g",
        "verified": False,
    }
