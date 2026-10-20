# ruff: noqa
from __future__ import annotations
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from ..records.narrow import as_dict, as_dict_or_none, as_int, as_str
from . import exclusions as exclusions_mod
from . import recorder as recorder_mod
from . import schema
from .sink import Sink

WINDOW = timedelta(hours=24)
NOTE = "losses that left no trace are not counted"
COUNTS = ("loss_lines", "refused", "disabled", "gaps", "malformed", "unknown_schema")


def _lines(path: Path) -> list[bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    lines = raw.split(b"\n")
    return [line for line in lines[:-1] if line] + ([lines[-1]] if lines[-1] else [])


def _events(
    sink: Sink, since: datetime, now: datetime, counts: dict[str, int]
) -> tuple[datetime | None, int]:
    excluded = exclusions_mod.load(sink)
    skipped = 0
    seqs: dict[str, set[int]] = {}
    newest: datetime | None = None
    try:
        files = sorted(sink.events.glob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day + timedelta(days=1) <= since.date() or day > now.date():
            continue
        for line in _lines(path):
            try:
                event = as_dict_or_none(json.loads(line))
            except ValueError:
                counts["malformed"] += 1
                continue
            if event is None:
                counts["malformed"] += 1
                continue
            version = event.get("schema")
            if type(version) is int and version != schema.SCHEMA_VERSION:
                counts["unknown_schema"] += 1
                continue
            if schema.validate(event) is not None:
                counts["malformed"] += 1
                continue
            when = schema.parse_ts(event["ts"])
            if when is None or not since <= when <= now:
                continue
            if excluded.skip(event):
                skipped += 1
                continue
            writer = as_dict(event["writer"])
            seqs.setdefault(as_str(writer["instance"]), set()).add(as_int(writer["seq"]))
            if event.get("kind") != "health.probe" and (newest is None or when > newest):
                newest = when
    for numbers in seqs.values():
        ordered = sorted(numbers)
        counts["gaps"] += sum((b - a - 1 for a, b in zip(ordered, ordered[1:], strict=False)))
    return (newest, skipped)


def _losses(sink: Sink, since: datetime, now: datetime, counts: dict[str, int]) -> None:
    for line in _lines(sink.loss):
        try:
            loss = as_dict(json.loads(line))
        except ValueError:
            continue
        when = schema.parse_ts(loss.get("ts"))
        if when is None or not since <= when <= now:
            continue
        if loss.get("kind_lost") == "disabled":
            counts["disabled"] += 1
        elif loss.get("reason") == "refused":
            counts["refused"] += 1
        else:
            counts["loss_lines"] += 1


def health(
    sink: Sink,
    *,
    now: datetime | None = None,
    window: timedelta = WINDOW,
    recorder: recorder_mod.Recorder | None = None,
) -> dict[str, Any]:
    rec = recorder or recorder_mod.process_recorder()
    probe = rec.record(sink, "health.probe") is not None
    now = now or datetime.now(UTC)
    since = now - window
    counts: dict[str, int] = dict.fromkeys(COUNTS, 0)
    newest = None
    skipped = 0
    try:
        newest, skipped = _events(sink, since, now, counts)
        _losses(sink, since, now, counts)
    except Exception:
        probe = False
    status = "failed" if not probe else "degraded" if any(counts.values()) else "ok"
    return {
        "status": status,
        "counts": counts,
        "newest": newest.isoformat() if newest else None,
        "window_s": window.total_seconds(),
        "excluded": skipped,
        "sink": str(sink.directory),
        "loss_file": str(sink.loss),
        "process": dict(rec.counts),
        "note": NOTE,
    }


def read_health(sink: Sink, since: datetime, until: datetime) -> dict[str, Any]:
    counts: dict[str, int] = dict.fromkeys(COUNTS, 0)
    try:
        newest, skipped = _events(sink, since, until, counts)
        _losses(sink, since, until, counts)
    except OSError:
        counts["malformed"] += 1
        newest, skipped = (None, 0)
    return {
        "counts": counts,
        "newest": newest.isoformat() if newest else None,
        "excluded": skipped,
        "note": NOTE,
    }
