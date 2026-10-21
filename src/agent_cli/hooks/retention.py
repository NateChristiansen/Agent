"""Machine-local full evidence, independent of record lifecycle and capture routing."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import core
from ..records.narrow import as_dict

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
    from .retention_sources import related as codex_related

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
