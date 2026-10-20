# ruff: noqa
from __future__ import annotations
import hashlib
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from ..core import AgentError, agent_parts
from ..lease_keys import area_key

_local = threading.local()
_registry_lock = threading.Lock()
_locks: dict[tuple[int, str, str], threading.RLock] = {}


def _create_lock(name: str, directory: int, deadline: float) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    while True:
        try:
            return os.open(name, flags, 384, dir_fd=directory)
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


@contextmanager
def area_guard(root: Path, area: str, *, timeout: float = 30):
    if os.name not in {"posix", "nt"}:
        raise AgentError("guarded dispatch/lease mutation requires supported file locking")
    storage_key = area_key(area)
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        from ..win_lock import is_reparse

        if is_reparse(root.lstat()):
            raise AgentError("harness mutation root must not be a reparse point")
    root = root.resolve(strict=True)
    key = (os.getpid(), str(root), storage_key)
    with _registry_lock:
        mutex = _locks.setdefault(key, threading.RLock())
    if not mutex.acquire(timeout=max(0, deadline - time.monotonic())):
        raise AgentError("harness mutation guard is busy; retry after the current operation")
    held: set[tuple[int, str, str]] | None = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set[tuple[int, str, str]]()
    fd = None
    try:
        if key in held:
            yield
            return
        name = hashlib.sha256(storage_key.encode()).hexdigest() + ".lock"
        if os.name == "nt":
            from ..win_lock import exclusive, open_lock_file

            fd = open_lock_file(root, agent_parts("leases", ".guards", name))
        else:
            import fcntl

            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in agent_parts("leases", ".guards"):
                    try:
                        os.mkdir(part, mode=448, dir_fd=directory)
                    except FileExistsError:
                        pass
                    following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = following
                fd = _create_lock(name, directory, deadline)
            finally:
                os.close(directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AgentError("harness mutation guard must be a regular file")
        if os.name == "nt":
            try:
                lock = exclusive(fd, timeout=max(0, deadline - time.monotonic()))
                lock.__enter__()
            except TimeoutError:
                raise AgentError(
                    "harness mutation guard is busy; retry after the current operation"
                ) from None
        else:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AgentError(
                            "harness mutation guard is busy; retry after the current operation"
                        ) from None
                    time.sleep(0.01)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            if os.name == "nt":
                lock.__exit__(None, None, None)
    finally:
        if fd is not None:
            os.close(fd)
        mutex.release()
