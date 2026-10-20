# ruff: noqa
from __future__ import annotations
import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast
from .schema import CLASSES, FIELDS, MODEL_KEYS


@dataclass
class Tally:
    status: str = "complete"
    reason: str = ""
    files: int = 0
    requests: int = 0
    by_class: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CLASSES, 0))
    models: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    unreadable: list[str] = field(default_factory=list[str])
    absent: str = ""
    "The data folder that was checked and does not exist; empty when it exists."

    def add(self, cls: str, model: str, input: int, output: int, cache_write: int, cache_read: int) -> None:
        row = self.models.setdefault(model, dict.fromkeys(MODEL_KEYS, 0))
        values = dict(zip(FIELDS, (input, output, cache_write, cache_read), strict=True))
        for key, value in values.items():
            row[key] += value
        row["requests"] += 1
        self.requests += 1
        self.by_class[cls] += sum(values.values())

    def finish(self) -> Tally:
        if self.unreadable and self.status == "complete":
            self.status = "partial"
            self.reason = f"{len(self.unreadable)} file(s) unreadable, first {self.unreadable[0]}"
        return self

    def source(self) -> dict[str, Any]:
        if self.status == "unavailable":
            return {"status": self.status, "reason": self.reason}
        out: dict[str, Any] = {"status": self.status, "files": self.files, "requests": self.requests}
        if self.reason:
            out["reason"] = self.reason
        if self.absent and (not self.files):
            out.update(absent=True, path=self.absent)
        return out


def local_date(stamp: object) -> date | None:
    try:
        parsed = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone().date()


def day_start(day: date) -> float:
    return dt.datetime.combine(day, dt.time.min).astimezone().timestamp()


def touched_since(path: Path, since: float) -> bool:
    try:
        return path.stat().st_mtime >= since
    except OSError:
        return False


def records(path: Path, tally: Tally) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield cast("dict[str, Any]", record)
    except (OSError, UnicodeDecodeError) as exc:
        tally.unreadable.append(f"{path}: {exc}")
