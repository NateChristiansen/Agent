"""A reused pid on Windows: a live process created after the store write that named the pid
is not the run's. Real processes and real verifier runs, as in test_verification_windows.py.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

import pytest

from agent_cli.dispatch import verification as runner
from agent_cli.dispatch import verification_status as status_mod
from agent_cli.dispatch.verification_store import Store
from test_verification_status import _rewrite
from verification_fixtures import config, fixture, start  # noqa: F401 — pytest fixture

native = pytest.mark.skipif(sys.platform != "win32", reason="native Windows process times")


@pytest.fixture
def stranger():
    """An unrelated live process, as a reused pid would be, with the time just before it."""
    before = time.time()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield process.pid, before
    finally:
        process.kill()
        process.wait()


def _deny(monkeypatch: pytest.MonkeyPatch, error: int) -> None:
    from agent_cli.dispatch import verification_win as win

    def refuse(_access: int, _inherit: bool, _pid: int) -> int:
        ctypes.set_last_error(error)  # type: ignore[attr-defined] — Windows-only API
        return 0

    monkeypatch.setattr(win._kernel32(), "OpenProcess", refuse)


def test_windows_status_passes_the_slack_to_the_windows_check(monkeypatch: pytest.MonkeyPatch):
    from agent_cli.dispatch import verification_win as win

    seen: list[tuple[int, float | None]] = []
    monkeypatch.setattr(status_mod.os, "name", "nt")
    monkeypatch.setattr(win, "process_gone", lambda pid, after: seen.append((pid, after)) or True)
    monkeypatch.setattr(win, "job_empty", lambda pgid: True)
    assert status_mod.process_gone(42, 100.0)
    assert status_mod.group_gone(43, 200.0)
    assert seen == [(42, 100.0 + status_mod.START_SLACK), (43, 200.0 + status_mod.START_SLACK)]


@native
def test_creation_time_is_on_the_wall_clock(stranger):
    from agent_cli.dispatch import verification_win as win

    pid, before = stranger
    kernel32 = win._kernel32()
    handle = kernel32.OpenProcess(0x1000, False, pid)
    assert handle
    try:
        started = win.created(handle)
    finally:
        kernel32.CloseHandle(handle)
    assert started is not None and before - 0.1 <= started <= time.time() + 0.1


@native
def test_a_process_created_after_since_is_a_reused_pid(stranger):
    pid, before = stranger
    assert status_mod.process_gone(pid, before - 60)
    assert not status_mod.process_gone(pid, None)


@native
def test_a_process_created_before_since_is_the_runs(stranger):
    pid, _ = stranger
    assert not status_mod.process_gone(pid, time.time())
    assert not status_mod.group_gone(pid, time.time())


@native
def test_an_exited_process_is_gone_whatever_since_says():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert status_mod.process_gone(process.pid, time.time() + 3600)


@native
def test_access_denied_keeps_the_number_only_answer(stranger, monkeypatch: pytest.MonkeyPatch):
    pid, before = stranger
    _deny(monkeypatch, 5)
    assert not status_mod.process_gone(pid, before - 60)
    assert not status_mod.group_gone(pid, before - 60)


@native
def test_an_unreadable_creation_time_keeps_the_number_only_answer(stranger, monkeypatch: pytest.MonkeyPatch):
    from agent_cli.dispatch import verification_win as win

    pid, before = stranger
    monkeypatch.setattr(win, "created", lambda handle: None)
    assert not status_mod.process_gone(pid, before - 60)


@native
def test_store_modified_reads_the_snapshot_time(fixture):  # noqa: F811 — the fixture
    handle = start(fixture, config("true"))
    assert runner.wait(handle).cleanup_confirmed
    store = Store(handle.root, handle.binding)
    try:
        past = time.time() - 3600
        os.utime(store.path / "lifecycle.json", (past, past))
        assert store.modified("lifecycle.json") == pytest.approx(past, abs=1e-3)
        assert store.modified("absent.json") is None
    finally:
        store.close()


@native
@pytest.mark.parametrize("role", ["guardian_pid", "command_pid", "watchdog_pid"])
@pytest.mark.parametrize("backdate", [True, False])
def test_a_finished_run_does_not_hold_the_lease_through_a_reused_pid(fixture, stranger, role, backdate):  # noqa: F811
    handle = start(fixture, config("true"))
    assert runner.wait(handle).cleanup_confirmed
    _rewrite(handle, backdate=backdate, **{role: stranger[0]})
    status = runner.load_status(handle.root, handle.binding)
    assert status.lifecycle["finished"] and status.lifecycle[role] == stranger[0]
    assert status.requires_lease is not backdate
    if backdate:
        assert not status.active and status.cleanup_confirmed
