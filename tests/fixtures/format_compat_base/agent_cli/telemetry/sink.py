# ruff: noqa
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
except ImportError:
    fcntl = None
LOCK_TIMEOUT = 0.25
WINDOWS_LOCK_TIMEOUT = 0.25
MAX_LINE = 64 * 1024
_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)


class Loss(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Sink:
    directory: Path
    key: Path

    @classmethod
    def for_main(cls, main: Path) -> Sink:
        return cls(agent_path(main, "telemetry"), main)

    @classmethod
    def from_directory(cls, directory: Path) -> Sink:
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
    digest = hashlib.sha256(str(key.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "agent-telemetry" / digest / "loss.jsonl"


@contextmanager
def locked(path: Path, timeout: float = LOCK_TIMEOUT) -> Generator[int]:
    fd = os.open(path, _FLAGS, 384)
    try:
        if os.name == "nt":
            from ..win_lock import exclusive

            guard = os.open(
                path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 384
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
    if len(line) > MAX_LINE:
        raise Loss("oversize")
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path) as fd:
        written = os.write(fd, line)
        if written != len(line):
            if 0 < written:
                try:
                    os.write(fd, b"\n")
                except OSError:
                    pass
            raise Loss("short_write")
