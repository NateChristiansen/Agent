# ruff: noqa
from __future__ import annotations
import ctypes
import subprocess
from ctypes import wintypes
from subprocess import Popen
from typing import Any


def _load_kernel32() -> ctypes.CDLL:
    return ctypes.WinDLL("kernel32", use_last_error=True)


_kernel32 = _load_kernel32()
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_ASSIGN_RIGHTS = 1 | 256


class Job:
    def __init__(self, process: Popen[Any] | int):
        handle: int | None = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _last_error()
        self._handle: int | None = handle
        borrowed = not isinstance(process, int)
        process_handle = (
            wintypes.HANDLE(process._handle)
            if borrowed
            else _kernel32.OpenProcess(_ASSIGN_RIGHTS, False, process)
        )
        try:
            if not process_handle:
                raise _last_error()
            if not _kernel32.AssignProcessToJobObject(handle, process_handle):
                raise _last_error()
        except OSError:
            self.close()
            raise
        finally:
            if not borrowed and process_handle:
                _kernel32.CloseHandle(process_handle)

    def terminate(self) -> None:
        if self._handle and (not _kernel32.TerminateJobObject(self._handle, 1)):
            raise _last_error()

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def kill_tree(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=10
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0
