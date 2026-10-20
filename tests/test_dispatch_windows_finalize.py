"""Windows supervisors that ended without writing their terminal state.

Three Windows faults: a rename over a file another process holds open fails (`core.replace`), a
child's inherited `"ab"` stderr overwrites the parent's own appends (`append_log.open_append`),
and an inventory with any unreadable row was never trusted, so a dead supervisor's worktree and
slot were held forever (`allocation.owned_pids`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import core
from agent_cli.append_log import open_append
from agent_cli.dispatch import allocation
from agent_cli.procs import Proc


def _flaky_replace(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    calls: list[int] = []

    def fake(src, dst):  # noqa: ANN001, ANN202 — stands in for os.replace
        calls.append(1)
        if len(calls) <= failures:
            raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(core.os, "replace", fake)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    return calls


def test_replace_retries_a_blocked_rename_on_windows(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    calls = _flaky_replace(monkeypatch, failures=3)
    core.replace("a", "b")
    assert len(calls) == 4


def test_replace_still_raises_once_the_retry_window_ends(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    calls = _flaky_replace(monkeypatch, failures=10**6)
    clock = iter(range(0, 1000))
    monkeypatch.setattr(core.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(PermissionError):
        core.replace("a", "b")
    assert len(calls) >= 1


def test_replace_never_sleeps_past_the_retry_window(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    _flaky_replace(monkeypatch, failures=10**6)
    now = [0.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(core.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    with pytest.raises(PermissionError):
        core.replace("a", "b")
    assert now[0] <= core.REPLACE_RETRY_SECONDS + 1e-9


def test_replace_does_not_retry_on_posix(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "linux")
    calls = _flaky_replace(monkeypatch, failures=1)
    with pytest.raises(PermissionError):
        core.replace("a", "b")
    assert len(calls) == 1


def test_atomic_write_survives_a_briefly_blocked_rename_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    real = core.os.replace
    blocked = [2]

    def fake(src, dst):  # noqa: ANN001, ANN202 — stands in for os.replace
        if blocked[0]:
            blocked[0] -= 1
            raise PermissionError(13, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(core.os, "replace", fake)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    target = tmp_path / "records" / "record.json"
    core.atomic_write(target, "{}\n")
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert [p.name for p in target.parent.iterdir()] == ["record.json"]


def test_a_child_appending_to_an_inherited_log_keeps_the_parents_lines(tmp_path):
    """The supervisor's stderr and `driver.note` both append to `.err`; neither may overwrite."""
    log = tmp_path / "x.err"
    log.write_bytes(b"before\n")
    with open_append(log) as stream:
        child = subprocess.Popen(  # noqa: S603 — this interpreter, fixed argv
            [sys.executable, "-c", "import sys,time; time.sleep(1.0); sys.stderr.write('child\\n')"],
            stdout=stream,
            stderr=stream,
        )
    with log.open("a", encoding="utf-8") as own:
        own.write("parent\n")
    assert child.wait(timeout=30) == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["before", "parent", "child"]


#: The dispatch in these records started an hour before `NOW`; rows say how long before `NOW`
#: they were created.
HOUR = 3600
NOW = time.time()
RECORD = {
    "id": "043-windows-02",
    "supervisor_pid": 500,
    "claude_pid": 501,
    "started": datetime.fromtimestamp(NOW - HOUR).astimezone().isoformat(timespec="seconds"),
}


def _row(pid: int, ppid: int, *, tag_error: str = "", age: float = 10 * HOUR) -> Proc:
    """A row as `win_snapshot` makes it when the creation clock cannot be opened: psutil's
    `created`, and `elapsed` truncated from it."""
    return Proc(
        pid=pid,
        ppid=ppid,
        pgid=pid,
        elapsed=str(int(age)),
        command="x",
        tag_error=tag_error,
        created=NOW - age,
    )


def _inventory(monkeypatch: pytest.MonkeyPatch, rows: list[Proc], platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(allocation, "_inventory", lambda: rows)
    if platform != "win32":
        monkeypatch.setattr(allocation.os, "getpgrp", lambda: 1, raising=False)


def test_windows_trusts_an_inventory_whose_unreadable_rows_are_unrelated(tmp_path, monkeypatch):
    """Protected system processes: unreadable, parents gone, started long before the dispatch."""
    rows = [_row(10, 4), _row(11, 4, tag_error="AccessDenied"), _row(12, 11, tag_error="NoSuchProcess")]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], True)


def test_windows_distrusts_an_unreadable_descendant_of_the_dispatch(tmp_path, monkeypatch):
    rows = [
        _row(10, 4),
        _row(600, 501, tag_error="AccessDenied", age=60),
        _row(601, 600, tag_error="AccessDenied", age=60),
    ]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD)[1] is False


def test_windows_distrusts_an_unreadable_grandchild_under_a_readable_child(tmp_path, monkeypatch):
    """The worker exited; its readable child lives on and has an unreadable child of its own."""
    rows = [_row(10, 4), _row(600, 501, age=60), _row(601, 600, tag_error="AccessDenied", age=60)]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([600, 601], False)


def test_windows_distrusts_an_unreadable_orphan_started_after_the_dispatch(tmp_path, monkeypatch):
    """Its parent is gone, so its chain cannot be followed; its start time keeps it a suspect."""
    rows = [_row(10, 4), _row(601, 600, tag_error="AccessDenied", age=60)]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], False)


def test_windows_distrusts_an_unreadable_child_whose_parent_pid_was_reused(tmp_path, monkeypatch):
    """Its parent 600 exited and Windows gave pid 600 to a newer, unrelated process."""
    rows = [_row(10, 4), _row(600, 4, age=30), _row(601, 600, tag_error="AccessDenied", age=60)]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], False)


def test_windows_trusts_an_unreadable_child_of_an_older_unrelated_parent(tmp_path, monkeypatch):
    rows = [_row(10, 4), _row(600, 4, age=2 * HOUR), _row(601, 600, tag_error="AccessDenied", age=60)]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], True)


def _filetime(seconds_ago: float) -> str:
    return str(int((NOW - seconds_ago + 11_644_473_600) * 10_000_000))


def test_windows_distrusts_a_reused_parent_hidden_by_a_rounded_age(tmp_path, monkeypatch):
    """The child's age truncates to the parent's: 60.9 s and 60.4 s both read as 60."""
    rows = [
        _row(10, 4),
        Proc(pid=600, ppid=4, pgid=600, elapsed="60", command="x", started=_filetime(60.4)),
        _row(601, 600, tag_error="AccessDenied", age=60.9),
    ]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], False)


@pytest.mark.parametrize("step", [-HOUR, -10, 0, 6, 10, HOUR])
def test_windows_ownership_does_not_move_with_a_later_clock(tmp_path, monkeypatch, step):
    """Starts come from the snapshot, so reading it later or after a clock step changes nothing."""
    rows = [
        _row(10, 4),
        Proc(pid=600, ppid=4, pgid=600, elapsed="60", command="x", started=_filetime(60.4)),
        _row(601, 600, tag_error="AccessDenied", age=60.9),
        _row(701, 501, tag_error="AccessDenied", age=HOUR - 0.4),
    ]
    _inventory(monkeypatch, rows, "win32")
    monkeypatch.setattr(time, "time", lambda: NOW + step)
    assert allocation.owned_pids(tmp_path, RECORD) == ([701], False)


def test_windows_follows_ancestry_past_a_readable_orphan(tmp_path, monkeypatch):
    """601 is readable and untagged; its parent exited and pid 600 now names a newer process."""
    rows = [
        _row(10, 4),
        _row(600, 4, age=30),
        _row(601, 600, age=60),
        _row(602, 601, tag_error="AccessDenied", age=10),
    ]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], False)


def test_windows_stops_ancestry_at_another_dispatchs_row(tmp_path, monkeypatch):
    other = Proc(
        pid=601, ppid=4, pgid=601, elapsed="60", command="x", dispatch="001-mac-02", created=NOW - 60
    )
    rows = [_row(10, 4), other, _row(602, 601, tag_error="AccessDenied", age=10)]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], True)


def test_windows_does_not_claim_a_child_of_a_gone_pid_older_than_the_dispatch(tmp_path, monkeypatch):
    """Pid 501 is the record's worker and is gone; 601 started before the dispatch did."""
    rows = [
        _row(10, 4),
        Proc(pid=601, ppid=501, pgid=601, elapsed="7200", command="x", started=_filetime(2 * HOUR)),
    ]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], True)


@pytest.mark.parametrize("before", [2.4, 299, 300])
def test_windows_does_not_claim_a_child_of_a_gone_pid_from_just_before_the_dispatch(
    tmp_path, monkeypatch, before
):
    started = Proc(pid=601, ppid=501, pgid=601, elapsed="0", command="x", started=_filetime(HOUR + before))
    _inventory(monkeypatch, [_row(10, 4), started], "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([], True)


def test_windows_claims_a_child_of_a_gone_worker_started_after_the_dispatch(tmp_path, monkeypatch):
    rows = [_row(10, 4), Proc(pid=601, ppid=501, pgid=601, elapsed="60", command="x", started=_filetime(60))]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD) == ([601], True)


def test_windows_distrusts_an_unreadable_orphan_with_no_known_start(tmp_path, monkeypatch):
    rows = [_row(10, 4), Proc(pid=601, ppid=600, pgid=601, elapsed="", command="x", tag_error="AccessDenied")]
    _inventory(monkeypatch, rows, "win32")
    assert allocation.owned_pids(tmp_path, RECORD)[1] is False


def test_posix_still_distrusts_any_unreadable_row(tmp_path, monkeypatch):
    rows = [_row(10, 4), _row(11, 4, tag_error="AccessDenied")]
    _inventory(monkeypatch, rows, "linux")
    assert allocation.owned_pids(tmp_path, {"id": "001-mac-01"}) == ([], False)


def test_a_patched_platform_never_reaches_the_windows_open(tmp_path, monkeypatch):
    """Tests elsewhere patch `sys.platform` to win32 on POSIX; `msvcrt` exists only on Windows."""
    monkeypatch.setattr(sys, "platform", "win32" if os.name != "nt" else "linux")
    log = Path(tmp_path / "plain.log")
    with open_append(log) as stream:
        stream.write(b"one\n")
    with open_append(log) as stream:
        stream.write(b"two\n")
    assert log.read_bytes() == b"one\ntwo\n"


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows handle path")
def test_a_failed_descriptor_wrap_closes_the_windows_handle(tmp_path, monkeypatch):
    import ctypes
    import msvcrt

    closed: list[int] = []

    def create_file(*args):  # noqa: ANN002, ANN202 — stands in for CreateFileW
        return 1234

    def close_handle(handle):  # noqa: ANN001, ANN202 — stands in for CloseHandle
        closed.append(handle)
        return 1

    fake = SimpleNamespace(CreateFileW=create_file, CloseHandle=close_handle)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake)

    def refuse(handle, flags):  # noqa: ANN001, ANN202 — stands in for open_osfhandle
        raise OSError("no descriptor")

    monkeypatch.setattr(msvcrt, "open_osfhandle", refuse)
    with pytest.raises(OSError, match="no descriptor"):
        open_append(tmp_path / "leak.log")
    assert closed == [1234]
