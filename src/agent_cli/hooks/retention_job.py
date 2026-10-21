"""Bounded enqueue, detached consumer, and crash-recoverable transcript refresh."""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..records.narrow import as_dict
from . import retention, retention_sources

TRIGGER_WAIT = 0.05
JOB_MAX = 600.0


@contextmanager
def lock(root: Path, name: str, wait: float) -> Generator[None]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    directory = root / retention.BASE
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from .. import win_lock

        fd = win_lock.open_lock_file(root, (*retention.BASE.parts, name))
        try:
            with win_lock.exclusive(fd, timeout=wait):
                yield
        finally:
            os.close(fd)
        return
    import fcntl

    with (directory / name).open("a") as fh:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(name) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def enqueue(root: Path, record: dict[str, Any]) -> None:
    from ..usage.job import spawn

    # Tests share the detached-job off switch. Synchronous entry points stay testable.
    if os.environ.get("AGENT_NO_USAGE") or os.environ.get("AGENT_NOW"):
        return
    record = {
        **{k: record.get(k) for k in ("session_id", "feature", "dispatch_id", "transcript_path")},
        "extra": {"agent_transcript_path": as_dict(record.get("extra")).get("agent_transcript_path")},
    }
    spool = root / retention.BASE / "pending" / f"{uuid.uuid4().hex}.json"

    def start(wait: float) -> None:
        spawn([sys.executable, "-m", "agent_cli.hooks.retention_job", str(root), str(wait)], root)

    try:
        with lock(root, "coord.lock", TRIGGER_WAIT):
            retention.save(spool, record)
            start(0)
    except TimeoutError:
        retention.save(spool, record)
        # A claimant must wait: the consumer may already have made its final queue read.
        start(JOB_MAX)


def run(root: Path, wait: float = 0) -> None:
    deadline = time.monotonic() + JOB_MAX
    try:
        writer = lock(root, "writer.lock", wait)
        writer.__enter__()
    except TimeoutError, FileNotFoundError:
        return
    try:
        threads = retention_sources.index(deadline)
        marker = root / retention.BASE / "backfill.json"
        if not marker.exists():
            retention.backfill(root, deadline, threads)
            retention.save(marker, {"status": "captured references scanned; not a census of conversations"})
        # Refresh known native sources too: later tails need not bring a new pointer.
        for path in list((root / retention.BASE / "reports").glob("*.json")):
            data = retention.load(path)
            if time.monotonic() >= deadline:
                raise TimeoutError("retention refresh deadline")
            for association in data.get("associations", []):
                record = {**association, "transcript_path": data.get("source")}
                retention.refresh(root, record, threads, deadline)
        while root.is_dir() and time.monotonic() < deadline:
            with lock(root, "coord.lock", max(0, deadline - time.monotonic())):
                pending = list((root / retention.BASE / "pending").glob("*.json"))
                if not pending:
                    # A trigger holds this same lock through enqueue and spawn. Release the
                    # writer before it can start a child, closing the final-read race.
                    writer.__exit__(None, None, None)
                    writer = None
                    return
            for path in pending:
                record = retention.load(path)
                retention.refresh(root, record, threads, deadline)
                path.unlink(missing_ok=True)
    except (OSError, TimeoutError) as exc:
        if root.is_dir():
            retention.save(root / retention.BASE / "failure.json", {"detail": str(exc)})
    finally:
        if writer is not None:
            writer.__exit__(None, None, None)


if __name__ == "__main__":
    run(Path(sys.argv[1]), float(sys.argv[2]))
