"""`core.is_alive` on Windows asks the kernel and never signals.

The native tests run only on windows-latest, where the Win32 calls are real. The routing tests
run everywhere: they fake the platform and prove the Windows branch never reaches `os.kill`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from agent_cli import core

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Win32 process queries")


def _fake_windows(monkeypatch: pytest.MonkeyPatch, answer: object) -> list[int]:
    """Pretend to be win32 with a stub `winproc`, and fail the test if `os.kill` is called."""
    asked: list[int] = []

    def pid_alive(pid: int) -> bool:
        asked.append(pid)
        if isinstance(answer, BaseException):
            raise answer
        return bool(answer)

    def no_kill(*_args: object) -> None:
        raise AssertionError("os.kill must not run on Windows")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "agent_cli.winproc", types.SimpleNamespace(pid_alive=pid_alive))
    monkeypatch.setattr(os, "kill", no_kill)
    return asked


def test_windows_routes_to_the_kernel_query_not_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake_windows(monkeypatch, True)
    assert core.is_alive(1234) is True
    assert core.is_alive("5678") is True  # type: ignore[arg-type]
    assert asked == [1234, 5678]


def test_windows_dead_pid_and_empty_values(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake_windows(monkeypatch, False)
    assert core.is_alive(1234) is False
    assert core.is_alive(None) is False
    assert core.is_alive(0) is False
    assert asked == [1234]


@pytest.mark.parametrize("bad", ["not-a-pid", 2**80])
def test_windows_bad_pid_is_not_alive(monkeypatch: pytest.MonkeyPatch, bad: object) -> None:
    _fake_windows(monkeypatch, OverflowError("too big") if isinstance(bad, int) else True)
    assert core.is_alive(bad) is False  # type: ignore[arg-type]


@windows_only
def test_native_own_process_is_alive() -> None:
    assert core.is_alive(os.getpid()) is True


@windows_only
def test_native_system_process_is_alive_through_access_denied() -> None:
    # pid 4 is System: OpenProcess is refused, and a refusal means it exists.
    assert core.is_alive(4) is True


@windows_only
def test_native_live_child_is_alive_and_not_interrupted() -> None:
    # A new process group on this console is exactly what os.kill(pid, 0) would Ctrl+C.
    child = subprocess.Popen(  # noqa: S603 — fixed argv, this interpreter
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
    )
    try:
        assert core.is_alive(child.pid) is True
        assert child.poll() is None, "the liveness check interrupted the child"
    finally:
        child.kill()
        child.wait(timeout=10)


@windows_only
def test_native_exited_child_is_dead() -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 — fixed argv
    finished.wait(timeout=10)
    # The Popen object still holds its handle, so the pid cannot be reused yet.
    assert core.is_alive(finished.pid) is False


@windows_only
def test_native_creation_time_identifies_live_process_and_expires() -> None:
    from agent_cli import winproc

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = winproc.pid_started(child.pid)  # pyright: ignore[reportAttributeAccessIssue] — win32-only name
        assert started.isdigit()
        assert winproc.pid_started(child.pid) == started  # pyright: ignore[reportAttributeAccessIssue]
    finally:
        child.kill()
        child.wait(timeout=10)
    assert winproc.pid_started(child.pid) == ""  # pyright: ignore[reportAttributeAccessIssue]


@windows_only
def test_native_toolhelp_inventory_keeps_pid_and_parent() -> None:
    from agent_cli import winproc

    python = getattr(sys, "_base_executable", sys.executable)
    child = subprocess.Popen([python, "-c", "import time; time.sleep(30)"])
    try:
        entries = winproc.process_entries()  # pyright: ignore[reportAttributeAccessIssue] — win32-only
        assert (child.pid, os.getpid()) in {(pid, ppid) for pid, ppid, _ in entries}
    finally:
        child.kill()
        child.wait(timeout=10)


class _FakeKernel:
    """Stands in for kernel32: a chosen OpenProcess result and exit-code read, and a record of
    every handle closed."""

    def __init__(self, handle: int, exit_ok: bool, code: int = 259) -> None:
        self.handle, self.exit_ok, self.code = handle, exit_ok, code
        self.closed: list[int] = []

    def OpenProcess(self, _access: int, _inherit: bool, _pid: int) -> int:  # noqa: N802 — Win32 name
        return self.handle

    def GetExitCodeProcess(self, _handle: int, code_ref: object) -> bool:  # noqa: N802 — Win32 name
        if self.exit_ok:
            code_ref._obj.value = self.code  # type: ignore[attr-defined]  # ctypes byref target
        return self.exit_ok

    def CloseHandle(self, handle: int) -> bool:  # noqa: N802 — Win32 name
        self.closed.append(handle)
        return True


@windows_only
@pytest.mark.parametrize(
    ("exit_ok", "code", "alive"),
    [(True, 259, True), (True, 0, False), (False, 0, True)],
)
def test_native_exit_code_read_and_handle_always_closed(
    monkeypatch: pytest.MonkeyPatch, exit_ok: bool, code: int, alive: bool
) -> None:
    # A failed exit-code read counts as alive: reaping a lease that may still run is worse.
    from agent_cli import winproc

    fake = _FakeKernel(handle=77, exit_ok=exit_ok, code=code)
    monkeypatch.setattr(winproc, "_kernel32", fake)
    assert winproc.pid_alive(1234) is alive  # pyright: ignore[reportAttributeAccessIssue] — win32-only name
    assert fake.closed == [77]


@windows_only
@pytest.mark.parametrize(("error", "alive"), [(5, True), (87, False)])
def test_native_open_failure_reads_the_error(
    monkeypatch: pytest.MonkeyPatch, error: int, alive: bool
) -> None:
    # Access denied (5) means the process exists; anything else (87: no such pid) means it does not.
    import ctypes

    from agent_cli import winproc

    fake = _FakeKernel(handle=0, exit_ok=False)
    monkeypatch.setattr(winproc, "_kernel32", fake)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error)
    assert winproc.pid_alive(1234) is alive  # pyright: ignore[reportAttributeAccessIssue] — win32-only name
    assert fake.closed == []


@windows_only
@pytest.mark.parametrize("pid", [-1, 2**32, 999_999_999])
def test_native_impossible_pids_are_dead(pid: int) -> None:
    assert core.is_alive(pid) is False
