"""Bounded Windows byte-range lock for cooperating harness processes.

The caller owns the lock file's lifetime. The same file and byte range must remain in place
for every cooperating writer; removing a lock file while it may be held breaks exclusion.
Path and reparse-point validation belong to the caller's storage-opening boundary.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from .core import AgentError


def is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _valid_part(part: str) -> bool:
    return bool(part) and part not in {".", ".."} and all(char not in part for char in "/\\:")


def directory_path(root: Path, parts: tuple[str, ...], *, create: bool) -> Path:
    """Walk storage directories without accepting existing reparse points."""
    if os.name != "nt":
        raise OSError("Windows storage requires Windows")
    if any(not _valid_part(part) for part in parts):
        raise AgentError("invalid Windows lock path")
    if is_reparse(root.lstat()):
        raise AgentError("harness mutation root must not be a reparse point")
    directory = root.resolve(strict=True)
    for part in parts:
        directory = directory / part
        if create:
            directory.mkdir(exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or is_reparse(info):
            raise AgentError("harness mutation guard directory must not be a reparse point")
    return directory


def _checked_regular(path: Path) -> os.stat_result | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or is_reparse(before):
        raise AgentError("harness mutation guard must be a regular file, not a reparse point")
    return before


def _same_file(fd: int, path: Path, before: os.stat_result | None) -> bool:
    opened = os.fstat(fd)
    after = path.lstat()
    return (
        stat.S_ISREG(opened.st_mode)
        and not is_reparse(after)
        and (opened.st_dev, opened.st_ino) == (after.st_dev, after.st_ino)
        and (before is None or (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino))
    )


def open_lock_file(root: Path, parts: tuple[str, ...]) -> int:
    """Open a stable lock file beneath root, rejecting Windows reparse points."""
    if len(parts) < 2 or not _valid_part(parts[-1]):
        raise AgentError("invalid Windows lock path")
    directory = directory_path(root, parts[:-1], create=True)
    path = directory / parts[-1]
    before = _checked_regular(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if not _same_file(fd, path, before):
            raise AgentError("harness mutation guard changed while opening")
        return fd
    except BaseException:
        os.close(fd)
        raise


OPEN_ATTEMPTS = 20
OPEN_RETRY_SECONDS = 0.01


class _Replaced(AgentError):
    pass


def open_regular_file(path: Path) -> int:
    """Open an existing regular file without accepting a reparse-point replacement.

    A writer's `os.replace` of the same path makes the open fail with PermissionError while the
    rename settles, or lands between the check and the open. Both are retried a bounded number
    of times, each attempt checked in full; the last attempt's error is raised. A non-regular
    file or reparse point found after the open is refused at once.
    """
    for _ in range(OPEN_ATTEMPTS - 1):
        try:
            return _open_regular(path)
        except PermissionError, _Replaced:
            time.sleep(OPEN_RETRY_SECONDS)
    return _open_regular(path)


def _open_regular(path: Path) -> int:
    before = _checked_regular(path)
    if before is None:
        raise FileNotFoundError(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        opened, after = os.fstat(fd), path.lstat()
        if not (stat.S_ISREG(opened.st_mode) and stat.S_ISREG(after.st_mode)) or is_reparse(after):
            raise AgentError("harness storage file must be a regular file, not a reparse point")
        if not (
            (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino) == (after.st_dev, after.st_ino)
        ):
            raise _Replaced("harness storage file changed while opening")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def exclusive(fd: int, *, timeout: float = 30) -> Generator[None]:
    """Lock byte zero of an open Windows file descriptor, then unlock that exact byte.

    ``LK_NBLCK`` returns immediately when another process owns the range. We retry until
    ``timeout`` so a slow mutation cannot hang a hook indefinitely. The handle close releases
    a lock after process death; explicit unlock keeps normal release prompt.
    """
    if os.name != "nt":
        raise OSError("Windows byte-range locking requires Windows")
    import msvcrt

    deadline = time.monotonic() + timeout
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            break
        except OSError as exc:
            if exc.errno not in {13, 36} and getattr(exc, "winerror", None) != 33:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows mutation lock is busy") from exc
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
