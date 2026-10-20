"""Inspect Windows processes with native APIs without opening visible helper consoles.

Never use os.kill(pid, 0) for Windows liveness: signal 0 maps to CTRL_C_EVENT and can signal a
console group. These queries request only PROCESS_QUERY_LIMITED_INFORMATION, close each handle
within its call and never signal. Import ctypes Windows APIs only on win32.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    MAX_PID = 0xFFFFFFFF
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

    def process_entries() -> list[tuple[int, int, str]]:
        """Atomic kernel inventory of PID, parent PID and executable name."""
        handle = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not _kernel32.Process32FirstW(handle, ctypes.byref(entry)):
                raise ctypes.WinError(ctypes.get_last_error())
            out: list[tuple[int, int, str]] = []
            while True:
                out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                if not _kernel32.Process32NextW(handle, ctypes.byref(entry)):
                    break
            return out
        finally:
            _kernel32.CloseHandle(handle)

    def pid_alive(pid: int) -> bool:
        """True while a process with this pid exists and has not exited.

        Access denied means the process exists and belongs to someone else (a service, another
        user, an elevated sandbox identity), matching POSIX's `PermissionError` answer. Any other
        open failure means no such process. A process whose exit code cannot be read is counted
        alive: the caller would otherwise reap a lease or a session that may still be running.
        A process that exits with code 259 reads as alive; Windows offers no way to tell it apart
        from a running one through this call, and nothing the harness launches exits with 259.
        """
        if pid <= 0 or pid > MAX_PID:
            return False
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    def pid_started(pid: int) -> str:
        """Creation clock for PID-reuse-safe keep records; empty if unavailable or dead."""
        if pid <= 0 or pid > MAX_PID:
            return ""
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
                return ""
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not _kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return ""
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            _kernel32.CloseHandle(handle)
