# ruff: noqa
from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import BinaryIO


def open_append(path: Path) -> BinaryIO:
    if os.name != "nt":
        return open(path, "ab")
    return _open_append_windows(path)


if sys.platform != "win32":

    def _open_append_windows(path: Path) -> BinaryIO:
        raise OSError(f"no Windows append handle for {path} on this platform")
else:

    def _open_append_windows(path: Path) -> BinaryIO:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        file_append_data, synchronize = (4, 1048576)
        share_all = 1 | 2 | 4
        open_always, normal = (4, 128)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = kernel32.CreateFileW(
            str(path), file_append_data | synchronize, share_all, None, open_always, normal, None
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND)
        except BaseException:
            kernel32.CloseHandle(handle)
            raise
        try:
            return os.fdopen(fd, "ab")
        except BaseException:
            os.close(fd)
            raise
