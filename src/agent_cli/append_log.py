"""Open inherited child logs without overwriting concurrent appends.

POSIX O_APPEND applies to every holder. Windows CRT ab seeks before parent writes but gives
inherited handles no append right, so a child can overwrite later writes at its inherited offset.
Use FILE_APPEND_DATA without plain write access to make kernel append atomic for every holder.

Select by the interpreter's real os.name so sys.platform patches cannot load msvcrt on POSIX. Keep
the Windows body under a platform check for cross-platform typing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import BinaryIO


def open_append(path: Path) -> BinaryIO:
    """`path` opened for appending, in a form a child inherits with append semantics."""
    if os.name != "nt":
        return open(path, "ab")  # noqa: SIM115 — the caller owns the stream and closes it
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
        file_append_data, synchronize = 0x0004, 0x00100000
        share_all = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        open_always, normal = 4, 0x80
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
        # From here the descriptor owns the handle, and closing the stream closes both.
        try:
            return os.fdopen(fd, "ab")
        except BaseException:
            os.close(fd)
            raise
