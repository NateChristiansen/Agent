"""The suite never reaches the owner's real Obsidian (owner report, 2026-10-03).

While the suite ran on the owner's Mac, Obsidian icons appeared in the Dock: the Obsidian CLI
is the app binary, found on PATH at /Applications/Obsidian.app/Contents/MacOS/obsidian, and
`obsidian eval` from the tab relabel and the drainer started the app. These tests hold the
guard in tests/obsidian_guard.py to that: a test finds only the recording stub, in process and
in a child process, and an attempt on the real app is refused and recorded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import obsidian, tab_drain
from obsidian_guard import REAL_OBSIDIAN, ObsidianGuard, obsidian_target

APP_BINARY = "/Applications/Obsidian.app/Contents/MacOS/Obsidian"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the recording stubs are POSIX scripts")


def _not_the_app(found: str | None) -> bool:
    return bool(found) and not REAL_OBSIDIAN.search(os.path.realpath(str(found)))


@posix_only
def test_the_obsidian_cli_a_test_finds_is_the_recording_stub() -> None:
    found = obsidian.cli_path()
    assert _not_the_app(found), f"a test resolved the real Obsidian: {found}"
    assert _not_the_app(shutil.which("obsidian")), shutil.which("obsidian")
    assert _not_the_app(shutil.which("open")), shutil.which("open")
    assert obsidian.run_cli(["version"], require_running=False)["ok"] is True
    calls_dir = Path(os.environ["OBSIDIAN_STUB_DIR"])
    calls = [json.loads(p.read_text(encoding="utf-8")) for p in calls_dir.iterdir()]
    assert [c["argv"][1:] for c in calls] == [["version"]]


def test_path_never_resolves_the_real_obsidian() -> None:
    """On Windows `obsidian` resolves through PATHEXT to the install's `obsidian.COM`, ahead of the
    extensionless stub, unless that folder leaves PATH."""
    found = shutil.which("obsidian")
    assert found is None or _not_the_app(found), found
    assert _not_the_app(obsidian.cli_path())


@posix_only
def test_a_child_process_finds_the_stub_too(obsidian_guard: ObsidianGuard) -> None:
    """The tab drainer runs detached, outside every monkeypatch; it must still miss the app."""
    probe = "from agent_cli import obsidian; print(obsidian.cli_path())"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(obsidian_guard.stub)


@pytest.mark.parametrize(
    "argv",
    [
        [APP_BINARY, "version"],
        ["/Applications/Obsidian.app/Contents/MacOS/obsidian", "eval", "code=1"],
        ["open", "-a", "Obsidian"],
        ["/usr/bin/open", "obsidian://open?vault=x"],
        "open -a Obsidian",
        ["C:\\Users\\x\\AppData\\Local\\Obsidian\\obsidian.COM", "eval", "code=1"],  # Windows console CLI
    ],
)
def test_spawning_the_real_app_is_refused_and_recorded(obsidian_guard: ObsidianGuard, argv) -> None:
    with pytest.raises(OSError, match="refused to start the real Obsidian"):
        subprocess.run(argv, shell=isinstance(argv, str), capture_output=True, check=False)
    assert len(obsidian_guard.blocked) == 1
    obsidian_guard.blocked.clear()  # the guard would otherwise fail this test at teardown


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    "line",
    [
        '"C:\\Users\\x\\AppData\\Local\\Obsidian\\obsidian.COM" eval code=1',
        'cmd.exe /c "C:\\Users\\x\\AppData\\Local\\Obsidian\\obsidian.com" eval code=1',
    ],
    ids=["direct", "cmd-wrapped"],
)
def test_a_command_line_naming_the_windows_cli_is_refused(
    obsidian_guard: ObsidianGuard, line: str, shell: bool
) -> None:
    with pytest.raises(OSError, match="refused to start the real Obsidian"):
        subprocess.run(line, shell=shell, capture_output=True, check=False)
    assert len(obsidian_guard.blocked) == 1
    obsidian_guard.blocked.clear()


@pytest.mark.parametrize(
    "args",
    ["version extra", ["git", "status"], "git"],
    ids=["shell-string-argv", "benign-argv0", "bare-string"],
)
def test_an_obsidian_executable_override_is_refused(obsidian_guard: ObsidianGuard, args) -> None:
    """`executable=` names what actually runs, whatever argv says (review finding)."""
    assert obsidian_target(args, executable=APP_BINARY) == APP_BINARY
    with pytest.raises(OSError, match="refused to start the real Obsidian"):
        subprocess.Popen(args, executable=APP_BINARY)
    assert len(obsidian_guard.blocked) == 1
    obsidian_guard.blocked.clear()


def test_the_tab_drainer_eval_cannot_reach_the_app(
    obsidian_guard: ObsidianGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real path from the report: `_eval` runs `obsidian eval` with whatever CLI it found."""
    monkeypatch.setattr(obsidian, "cli_path", lambda: APP_BINARY)
    # A refused spawn is a failed eval, not "no tab found" (see `tab_drain.EVAL_FAILURE_LIMIT`).
    assert tab_drain._eval([{"key": "k", "match": "m", "label": "L", "generated": True}]) is None
    assert obsidian_guard.blocked and obsidian_guard.blocked[0][0] == APP_BINARY
    obsidian_guard.blocked.clear()


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        [sys.executable, "-c", "pass"],
        ["/tmp/fakebin/obsidian", "open", "file=x"],  # a test's own fake CLI
        "echo obsidian",
        ["open", "README.md"],
    ],
)
def test_other_commands_pass(argv) -> None:
    assert obsidian_target(argv) == ""
