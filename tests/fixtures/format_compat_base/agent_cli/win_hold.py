# ruff: noqa
from __future__ import annotations
import math
import os
from collections.abc import Generator
from contextlib import contextmanager


def windows() -> bool:
    return os.name == "nt"


@contextmanager
def hold(fd: int, *, timeout: float | None = None) -> Generator[None]:
    from . import win_lock

    with win_lock.exclusive(fd, timeout=math.inf if timeout is None else timeout):
        yield


@contextmanager
def blocking(fd: int) -> Generator[None]:
    if windows():
        with hold(fd):
            yield
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)
    yield
