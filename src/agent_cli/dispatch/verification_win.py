"""Windows verifier process ownership: kill-on-close jobs assigned before the child runs.

Every verifier child is created suspended, assigned to a job with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, and only then resumed. A child that cannot be owned is
terminated while still suspended and the launch refuses; there is no PID-tracking fallback.
A command's job is named for its PID so the watchdog and status readers can open it; when its
last handle closes, Windows ends every process left in it. Same-user tampering with the named
object is outside the contract, as it is for POSIX process groups.
"""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import sys
import time
from ctypes import wintypes
from functools import cache
from pathlib import Path
from typing import Any

from ..core import AgentError

_JOB_KILL_ON_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_QUERY_TERMINATE = 0x0004 | 0x0008
_PROCESS_QUERY_SYNCHRONIZE = 0x1000 | 0x00100000
_ERROR_FILE_NOT_FOUND = 2
_ERROR_INVALID_PARAMETER = 87
_ERROR_ALREADY_EXISTS = 183
_ERROR_BROKEN_PIPE = 109
_PIPE_POLL = 0.005
_UNIX_EPOCH_TICKS = 116444736000000000  # 1970-01-01 in 100 ns FILETIME ticks since 1601
SPAWN_FLAGS = 0x00000004 | 0x08000000 | 0x00000200  # CREATE_SUSPENDED | NO_WINDOW | NEW_PROCESS_GROUP
HELPER_FLAGS = 0x08000000 | 0x00000200


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _load(name: str, *, use_last_error: bool) -> ctypes.CDLL:
    return ctypes.WinDLL(name, use_last_error=use_last_error)  # type: ignore[attr-defined] — Windows-only API


@cache
def _kernel32() -> ctypes.CDLL:
    dll = _load("kernel32", use_last_error=True)
    dll.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    dll.CreateJobObjectW.restype = wintypes.HANDLE
    dll.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    dll.OpenJobObjectW.restype = wintypes.HANDLE
    dll.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    dll.SetInformationJobObject.restype = wintypes.BOOL
    dll.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    dll.QueryInformationJobObject.restype = wintypes.BOOL
    dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL
    dll.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.TerminateJobObject.restype = wintypes.BOOL
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    dll.GetProcessTimes.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    return dll


@cache
def _ntdll() -> ctypes.CDLL:
    dll = _load("ntdll", use_last_error=False)
    dll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    dll.NtResumeProcess.restype = ctypes.c_long
    return dll


def _error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined] — Windows-only API


def job_name(pid: int) -> str:
    return f"Local\\agent-verifier-{pid}"


class Job:
    """One job handle. Closing the last handle to a job ends every process still in it."""

    def __init__(self, handle: int):
        self.handle: int | None = handle

    def active(self) -> int:
        info = _Accounting()
        if not _kernel32().QueryInformationJobObject(
            self.handle, _BASIC_ACCOUNTING_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise _error()
        return int(info.ActiveProcesses)

    def terminate(self) -> None:
        if self.handle and not _kernel32().TerminateJobObject(self.handle, 1):
            raise _error()

    def close(self) -> None:
        if self.handle:
            _kernel32().CloseHandle(self.handle)
            self.handle = None


def create(name: str | None) -> Job:
    """A new kill-on-close job; an existing job under `name` is refused, never joined."""
    kernel32 = _kernel32()
    ctypes.set_last_error(0)  # type: ignore[attr-defined] — Windows-only API
    handle = kernel32.CreateJobObjectW(None, name)
    if not handle:
        raise _error()
    job = Job(handle)
    if name is not None and ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:  # type: ignore[attr-defined]
        job.close()
        raise AgentError("a verifier job with this name already exists")
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
    if not kernel32.SetInformationJobObject(
        handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
    ):
        error = _error()
        job.close()
        raise error
    return job


def open_named(pid: int) -> Job | None:
    """The command job named for `pid`, or None once every handle to it has closed."""
    handle = _kernel32().OpenJobObjectW(_JOB_QUERY_TERMINATE, False, job_name(pid))
    if handle:
        return Job(handle)
    if ctypes.get_last_error() == _ERROR_FILE_NOT_FOUND:  # type: ignore[attr-defined]
        return None
    raise _error()


def assign(job: Job, process: subprocess.Popen[bytes]) -> None:
    if not _kernel32().AssignProcessToJobObject(job.handle, int(process._handle)):  # type: ignore[attr-defined]
        raise _error()


def resume(process: subprocess.Popen[bytes]) -> None:
    """Resume the suspended primary thread. `Popen` closes its thread handle, so this resumes
    through the process handle it keeps, which cannot name a different process."""
    status = _ntdll().NtResumeProcess(int(process._handle))  # type: ignore[attr-defined]
    if status != 0:
        raise OSError(f"NtResumeProcess failed with status {status & 0xFFFFFFFF:#010x}")


def spawn(
    argv: list[str], *, named: bool, cwd: Path, env: dict[str, str], stdin: Any, stdout: Any, stderr: Any
) -> tuple[subprocess.Popen[bytes], Job]:
    """Start `argv` suspended, own it with a kill-on-close job, then let it run."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        creationflags=SPAWN_FLAGS,
        close_fds=True,
    )
    job = None
    try:
        job = create(job_name(process.pid) if named else None)
        assign(job, process)
        resume(process)
        return process, job
    except BaseException as exc:
        process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        if job is not None:
            job.close()
        if isinstance(exc, (OSError, AgentError)):
            raise AgentError(
                "verifier could not own its child process with a job; refusing to run it"
            ) from exc
        raise


def created(handle: int) -> float | None:
    """A process's creation time in Unix seconds, or None when it cannot be read."""
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel32().GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    ticks = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    return (ticks - _UNIX_EPOCH_TICKS) / 10_000_000 if ticks else None


def process_gone(pid: int, after: float | None) -> bool:
    """True once `pid` has exited or no longer exists, or when the live process under it was
    created after `after` (Unix seconds) and so holds a reused number. False when it cannot be
    told: access denied, or a creation time that cannot be read, leaves a live pid alive."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER  # type: ignore[attr-defined]
    try:
        if kernel32.WaitForSingleObject(handle, 0) == 0:
            return True
        if after is None:
            return False
        started = created(handle)
        return started is not None and started > after
    finally:
        kernel32.CloseHandle(handle)


def process_dead(pid: int) -> bool:
    """True once `pid` has exited or no longer exists; False when it cannot be told."""
    return process_gone(pid, None)


def job_empty(pid: int) -> bool:
    """No process remains in the job named for `pid` (also true once the job is gone)."""
    job = open_named(pid)
    if job is None:
        return True
    try:
        return job.active() == 0
    finally:
        job.close()


def terminate_named(pid: int) -> bool:
    """End the job named for `pid`; False when there is no such job."""
    job = open_named(pid)
    if job is None:
        return False
    try:
        job.terminate()
    finally:
        job.close()
    return True


def pipe_ready(fd: int, timeout: float) -> bool:
    """`select` for one anonymous pipe: bytes waiting, or the writer gone (a read sees EOF)."""
    import _winapi
    import msvcrt

    handle: int = msvcrt.get_osfhandle(fd)  # type: ignore[attr-defined] — Windows-only API
    deadline = time.monotonic() + timeout
    while True:
        try:
            available, _ = _winapi.PeekNamedPipe(handle, 0)  # type: ignore[misc] — size 0 returns two values
        except BrokenPipeError:
            return True
        except OSError as exc:
            if getattr(exc, "winerror", None) == _ERROR_BROKEN_PIPE:
                return True
            raise
        if available:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PIPE_POLL, remaining))


def launch_python(
    code: str, *, owned: bool, cwd: Path, env: dict[str, str], stdout: Any
) -> tuple[subprocess.Popen[bytes], Job | None]:
    """A verifier Python role. A venv's python.exe is a launcher that runs the base interpreter
    as its child, so its PID is not the verifier's: run the base interpreter on the venv's site
    directories instead. Only an `owned` role (the command) goes into a named job."""
    sites = [path for path in sys.path if path.endswith("site-packages")]
    code = f"import site; [site.addsitedir(p) for p in {sites!r}]; {code}"
    argv = [getattr(sys, "_base_executable", sys.executable), "-I", "-c", code]
    stderr = subprocess.STDOUT if stdout == subprocess.PIPE else subprocess.DEVNULL
    if owned:
        return spawn(argv, named=True, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=stderr,
        creationflags=HELPER_FLAGS,
        close_fds=True,
    )
    return process, None


def capture(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float, limit: int, poll: float
) -> tuple[int, bytes]:
    """The Windows side of `verification_process.capture`. A kill-on-close job held only by
    this process stands in for the POSIX exec timer: the query dies with its Python parent even
    when that parent is killed outright."""
    proc, job = spawn(
        argv,
        named=False,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentError("verification observation timed out")
            if pipe_ready(proc.stdout.fileno(), min(poll, remaining)):
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise AgentError("verification observation exceeded its output bound")
        return proc.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(output)
    finally:
        try:
            job.terminate()
        finally:
            job.close()
        proc.wait(timeout=2)
        if proc.stdout:
            proc.stdout.close()


def gitlink_problem(worktree: Path, paths: set[bytes]) -> str | None:
    """The no-follow gitlink walk with `lstat`: a reparse point (symlink or junction) anywhere
    on the path is refused. Returns why the tree is unsupported, or None."""
    from ..win_lock import is_reparse

    for path in paths:
        parts = os.fsdecode(path).split("/")
        if any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
            return "invalid gitlink path"
        current = worktree
        info = current.lstat()
        if is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("verification worktree is a reparse point")
        for part in parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                break  # Git leaves unpopulated submodule directories absent or empty.
            if is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                return "gitlink path is not an unpopulated directory"
        else:
            with os.scandir(current) as entries:
                if next(entries, None) is not None:
                    return "populated gitlink/submodule is unsupported"
    return None
