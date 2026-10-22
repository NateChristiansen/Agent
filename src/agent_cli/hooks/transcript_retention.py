"""Machine-local transcript retention, detached refresh, and preservation reports.

`enqueue(root, record)` is the bounded integration API for capture and completion
callers. It accepts `session_id` (the native Codex thread id for rollout discovery),
`feature` (numeric id or None), `dispatch_id`, optional `transcript_path`, and optional
`extra.agent_transcript_path`. It queues references and launches a detached consumer;
it never scans history or copies transcript contents in the caller. OSError can be
handled by a fail-open completion caller; queued requests survive launch failure.

Raw capture events link `reports(record)` beneath `.agent/raw/retained/reports/`.
Reports distinguish preserved, partial, missing and unavailable evidence and link
native descendants. The archive is local; a backfill of captured references is not
a census of all conversations. Unknown native identities cannot be reconstructed.

Codex worker hooks do not run manager capture, and a killed Claude worker sends no
Stop, so `dispatch.terminal.finalize` enqueues every stopped worker itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .. import core
from ..records.narrow import as_dict
from ..usage.codex import FOLDERS, codex_home

BASE = core.agent_path(Path(), "raw", "retained")


def key(record: dict[str, Any], source: str) -> str:
    identity = [record.get("session_id"), source]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def references(record: dict[str, Any]) -> list[str]:
    extra = as_dict(record.get("extra"))
    return list(
        dict.fromkeys(
            str(p) for p in (record.get("transcript_path"), extra.get("agent_transcript_path")) if p
        )
    )


def reports(record: dict[str, Any]) -> list[str]:
    return [(BASE / "reports" / f"{key(record, p)}.json").as_posix() for p in references(record) or [""]]


def load(path: Path) -> dict[str, Any]:
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".retention-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=True)
            out.write("\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _fingerprint(stat: os.stat_result) -> dict[str, int]:
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino, "device": stat.st_dev}


def _copy(source: Path, dest: Path, deadline: float) -> tuple[dict[str, Any], bool]:
    from ..capture import REDACTED_RECORD_TYPES

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".retention-")
    partial = False
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            before = os.fstat(inp.fileno())
            remaining = before.st_size
            # Read only the snapshot available at open, even if the provider keeps appending.
            while remaining:
                if time.monotonic() >= deadline:
                    raise TimeoutError("retention pass deadline")
                parts: list[bytes] = []
                while remaining:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("retention pass deadline")
                    part = inp.readline(min(remaining, 1024 * 1024))
                    if not part:
                        raise OSError("source truncated during preservation")
                    remaining -= len(part)
                    parts.append(part)
                    if part.endswith(b"\n"):
                        break
                line = b"".join(parts)
                if not line.endswith(b"\n"):
                    # A partial credential record cannot safely be filtered. Keep the last
                    # published copy until a later trigger observes a complete record.
                    partial = True
                    break
                try:
                    record = as_dict(json.loads(line))
                except ValueError, UnicodeError:
                    record = {}
                if record.get("type") not in REDACTED_RECORD_TYPES:
                    out.write(line)
            after = os.fstat(inp.fileno())
            if after.st_size < before.st_size or (
                after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns
            ):
                raise OSError("source changed during preservation")
            partial = partial or after.st_size > before.st_size
        if partial and dest.exists():
            return _fingerprint(before), True
        os.replace(tmp, dest)
        return _fingerprint(before), partial
    finally:
        Path(tmp).unlink(missing_ok=True)


def retain(root: Path, record: dict[str, Any], *, deadline: float) -> list[Path]:
    """Called with the retention writer lock, never the capture/adoption lock."""
    copied: list[Path] = []
    for source in references(record) or [""]:
        ident = key(record, source)
        report = root / BASE / "reports" / f"{ident}.json"
        old = load(report)
        dest = root / BASE / "transcripts" / f"{ident}.jsonl"
        associations = old.get("associations", [])
        association = {k: record.get(k) for k in ("session_id", "feature", "dispatch_id")}
        if association not in associations:
            associations.append(association)
        data = {
            **old,
            "source": source or None,
            "session_id": record.get("session_id"),
            "associations": associations,
            "checked_at": core.iso(core.now()),
            "preserved": dest.relative_to(root).as_posix() if dest.exists() else None,
        }
        try:
            if not source:
                data.update(status="unavailable", detail="capture supplied no native transcript path")
            else:
                stat = Path(source).stat()
                fingerprint = _fingerprint(stat)
                if (
                    dest.exists()
                    and old.get("fingerprint") == fingerprint
                    and old.get("status") == "preserved"
                ):
                    data.update(status="preserved", detail="")
                else:
                    fingerprint, partial = _copy(Path(source), dest, deadline)
                    data.update(
                        status="partial" if partial else "preserved",
                        detail="in-progress final record; refresh owed" if partial else "",
                        fingerprint=fingerprint,
                        preserved=dest.relative_to(root).as_posix(),
                    )
                    copied.append(dest)
        except TimeoutError:
            raise
        except (OSError, UnicodeError) as exc:
            data.update(
                status="missing" if isinstance(exc, FileNotFoundError) else "unavailable", detail=str(exc)
            )
        save(report, data)
    return copied


def related(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Claude native sidechains can exist even without a delivered SubagentStop."""
    out = [record]
    for source in references(record):
        path = Path(source)
        for child in (path.parent / path.stem / "subagents").glob("*.jsonl"):
            out.append({**record, "transcript_path": str(child), "extra": {}})
    return out


def refresh(
    root: Path, record: dict[str, Any], threads: dict[str, list[tuple[Path, str]]], deadline: float
) -> None:
    available = related(record) + codex_related(record, threads)
    linked = sorted({p for child in available for p in reports(child)} - set(reports(record)))
    for child in available:
        for attempt in range(3):
            retain(root, child, deadline=deadline)
            if not any(load(root / p).get("status") == "partial" for p in reports(child)):
                break
            if attempt < 2:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    for path in reports(record):
        data = load(root / path)
        data["related_reports"] = linked
        if not data.get("source") and linked:
            data.update(status="discovered", detail="native sources found; see related_reports")
        save(root / path, data)


def backfill(root: Path, deadline: float, threads: dict[str, list[tuple[Path, str]]] | None = None) -> None:
    """Only the detached consumer scans captured history; it makes no completeness claim."""
    from ..capture import read_jsonl
    from ..sessions import get

    for path in sorted(core.agent_path(root, "raw").glob("*/*.jsonl")):
        for record in read_jsonl(path):
            if time.monotonic() >= deadline:
                raise TimeoutError("retention backfill deadline")
            refresh(root, record, threads or {}, deadline)
            sid = record.get("session_id")
            mapped = get(root, str(sid)) if sid else None
            feature = as_dict(as_dict(mapped).get("feature")).get("id")
            if feature and feature != record.get("feature"):
                retain(root, {**record, "feature": feature}, deadline=deadline)


def native_index(deadline: float) -> dict[str, list[tuple[Path, str]]]:
    threads: dict[str, list[tuple[Path, str]]] = {}
    for folder in FOLDERS:
        for path in (codex_home(Path.home()) / folder).glob("**/rollout-*.jsonl"):
            if time.monotonic() >= deadline:
                raise TimeoutError("native transcript discovery deadline")
            try:
                with path.open("rb") as inp:
                    line = inp.readline(1024 * 1024)
                record = as_dict(json.loads(line))
            except OSError, ValueError, UnicodeError:
                continue
            if record.get("type") != "session_meta":
                continue
            meta = as_dict(record.get("payload"))
            ident = str(meta.get("id") or "")
            spawn = as_dict(as_dict(as_dict(meta.get("source")).get("subagent")).get("thread_spawn"))
            parent = str(meta.get("parent_thread_id") or spawn.get("parent_thread_id") or "")
            if ident:
                threads.setdefault(ident, []).append((path, parent))
    return threads


def codex_related(record: dict[str, Any], threads: dict[str, list[tuple[Path, str]]]) -> list[dict[str, Any]]:
    sid = str(record.get("session_id") or "")
    if not sid or not threads:
        return []
    selected = {sid}
    while True:
        children = {
            ident for ident, paths in threads.items() if any(parent in selected for _, parent in paths)
        }
        if children <= selected:
            break
        selected.update(children)
    return [
        {**record, "transcript_path": str(path), "extra": {}, "native_session_id": ident}
        for ident in selected
        for path, _ in threads.get(ident, [])
    ]


TRIGGER_WAIT = 0.05
JOB_MAX = 600.0


@contextmanager
def lock(root: Path, name: str, wait: float) -> Generator[None]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    directory = root / BASE
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from .. import win_lock

        fd = win_lock.open_lock_file(root, (*BASE.parts, name))
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
    """Queue native references for detached retention; see the module integration contract."""
    from ..usage.job import spawn

    # Tests share the detached-job off switch. Synchronous entry points stay testable.
    if os.environ.get("AGENT_NO_USAGE") or os.environ.get("AGENT_NOW"):
        return
    record = {
        **{k: record.get(k) for k in ("session_id", "feature", "dispatch_id", "transcript_path")},
        "extra": {"agent_transcript_path": as_dict(record.get("extra")).get("agent_transcript_path")},
    }
    spool = root / BASE / "pending" / f"{uuid.uuid4().hex}.json"

    def start(wait: float) -> None:
        spawn([sys.executable, "-m", "agent_cli.hooks.transcript_retention", str(root), str(wait)], root)

    try:
        with lock(root, "coord.lock", TRIGGER_WAIT):
            save(spool, record)
            start(0)
    except TimeoutError:
        save(spool, record)
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
        threads = native_index(deadline)
        marker = root / BASE / "backfill.json"
        if not marker.exists():
            backfill(root, deadline, threads)
            save(marker, {"status": "captured references scanned; not a census of conversations"})
        # Refresh known native sources too: later tails need not bring a new pointer.
        for path in list((root / BASE / "reports").glob("*.json")):
            data = load(path)
            if time.monotonic() >= deadline:
                raise TimeoutError("retention refresh deadline")
            for association in data.get("associations", []):
                record = {**association, "transcript_path": data.get("source")}
                refresh(root, record, threads, deadline)
        while root.is_dir() and time.monotonic() < deadline:
            with lock(root, "coord.lock", max(0, deadline - time.monotonic())):
                pending = list((root / BASE / "pending").glob("*.json"))
                if not pending:
                    # A trigger holds this same lock through enqueue and spawn. Release the
                    # writer before it can start a child, closing the final-read race.
                    writer.__exit__(None, None, None)
                    writer = None
                    return
            for path in pending:
                record = load(path)
                refresh(root, record, threads, deadline)
                path.unlink(missing_ok=True)
    except (OSError, TimeoutError) as exc:
        if root.is_dir():
            save(root / BASE / "failure.json", {"detail": str(exc)})
    finally:
        if writer is not None:
            writer.__exit__(None, None, None)


if __name__ == "__main__":
    run(Path(sys.argv[1]), float(sys.argv[2]))
