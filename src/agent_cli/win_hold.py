"""Windows side of the harness's whole-file flock sites (board, ledger, land journal)."""

from __future__ import annotations

import math
import os
from collections.abc import Generator
from contextlib import contextmanager


def windows() -> bool:
    return os.name == "nt"


@contextmanager
def hold(fd: int, *, timeout: float | None = None) -> Generator[None]:
    """Hold `win_lock.exclusive` on `fd`. `timeout=None` waits as long as `flock(LOCK_EX)` would
    (`exclusive` alone gives up after 30 s); a number raises `TimeoutError` after that long."""
    from . import win_lock

    with win_lock.exclusive(fd, timeout=math.inf if timeout is None else timeout):
        yield


@contextmanager
def blocking(fd: int) -> Generator[None]:
    """Wait for an exclusive lock on `fd` as long as it takes: `flock(LOCK_EX)` on POSIX, released
    when the descriptor closes; the byte-range lock on Windows."""
    if windows():
        with hold(fd):
            yield
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)
    yield
