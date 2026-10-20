"""Append typed telemetry to the local sink without failing the caller. Reject test writes outside
their temp fence and retain loss evidence when recording cannot complete.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..core import agent_path

try:
    import fcntl
except ImportError:  # Windows uses win_lock instead
    fcntl = None  # type: ignore[assignment]

# An append holds the lock for microseconds, so the budget only matters under contention. 50 ms
# was too tight on a loaded Mac: eight writers lost one event in 500 during two landing full
# suites, which is the same drop a busy machine causes in production.
LOCK_TIMEOUT = 0.25
# Windows retries the byte-range lock every 10 ms, which its ~15.6 ms timer turns into about three
# tries in 50 ms; eight concurrent writers then lost one event in 500 (CI).
WINDOWS_LOCK_TIMEOUT = 0.25
MAX_LINE = 64 * 1024
_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)


class Loss(Exception):
    """A write that did not land whole; `kind` is the loss kind recorded for it."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Sink:
    """`directory` is `<main>/.agent/telemetry`; `key` names the loss directory for it."""

    directory: Path
    key: Path

    @classmethod
    def for_main(cls, main: Path) -> Sink:
        return cls(agent_path(main, "telemetry"), main)

    @classmethod
    def from_directory(cls, directory: Path) -> Sink:
        """A sink named by `AGENT_TELEMETRY_SINK`, keyed by its main checkout when it has the
        usual `<main>/.agent/telemetry` shape."""
        if directory == agent_path(directory.parent.parent, "telemetry"):
            return cls(directory, directory.parent.parent)
        return cls(directory, directory)

    @property
    def events(self) -> Path:
        return self.directory / "events"

    def day(self, day: date) -> Path:
        return self.events / f"{day.isoformat()}.jsonl"

    @property
    def loss(self) -> Path:
        return loss_path(self.key)


def loss_path(key: Path) -> Path:
    """`<tempdir>/agent-telemetry/<sha256(main)[:16]>/loss.jsonl`, read at call time so a test can
    point `tempfile.tempdir` elsewhere."""
    digest = hashlib.sha256(str(key.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "agent-telemetry" / digest / "loss.jsonl"


@contextmanager
def locked(path: Path, timeout: float = LOCK_TIMEOUT) -> Generator[int]:
    """Open `path` for appending and hold its exclusive lock; yields the append descriptor.

    POSIX flocks the file itself, retrying LOCK_NB to the deadline. Windows locks a sibling
    `.lock` file instead: msvcrt locks are mandatory, so locking the data file would make a
    concurrent reader fail.
    """
    fd = os.open(path, _FLAGS, 0o600)
    try:
        if os.name == "nt":
            from ..win_lock import exclusive

            guard = os.open(
                path.with_name(path.name + ".lock"),
                os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                with exclusive(guard, timeout=WINDOWS_LOCK_TIMEOUT if timeout == LOCK_TIMEOUT else timeout):
                    yield fd
            finally:
                os.close(guard)
            return
        if fcntl is None:
            raise OSError("telemetry requires file locking")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("telemetry lock is busy") from None
                time.sleep(0.001)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append(path: Path, line: bytes) -> None:
    """Append one encoded line with one `os.write` under the lock. Raises `Loss` for an
    oversize line or a short write, and the OS error or `TimeoutError` otherwise."""
    if len(line) > MAX_LINE:
        raise Loss("oversize")
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path) as fd:
        written = os.write(fd, line)
        if written != len(line):
            # End the partial line while the lock is still held, so the reader counts one
            # malformed line instead of gluing it onto the next writer's event.
            if 0 < written:
                try:
                    os.write(fd, b"\n")
                except OSError:
                    pass
            raise Loss("short_write")
