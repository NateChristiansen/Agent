"""One pass of this machine's usage publication: plan the days, collect, install.

A day is collected again while any source's checkpoint mark is before its end, or while its
record is not `complete`. Nothing is finalized by age. Collection runs outside every lock and
stops starting new days once the budget is spent; the days it did not reach stay behind the
checkpoint, so the next pass collects them. The checkpoint moves only over days collected in
order, and a source that came back `partial` or `unavailable` stops its own mark there. A
stopped source is retried at its own mark, and does not hold back the days the others still
need.

The outcome's `pending` names the work still owed: days not reached, a `partial` source, and
an `unavailable` source whose evidence changed since the last checkpoint. An `unavailable`
source with the same mark and the same `reason` on that day's record as last time is
`settled`, not pending: retrying it would change nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .. import core
from ..records.narrow import as_dict
from .collect import DAY_OPEN, collect_day, day_end
from .files import base, checkpoint_rel, own_checkpoint, own_records, record_rel, render
from .install import install
from .schema import CHECKPOINT_TAG, SCHEMA, SOURCES, checkpoint_problems, record_problems, stamp

BACKFILL_DAYS = 30
BUDGET_S = 90.0
OK = ("written", "committed", "current", "pushed")


@dataclass
class Outcome:
    status: str
    detail: str = ""
    collected: list[str] = field(default_factory=list[str])
    left: list[str] = field(default_factory=list[str])
    written: list[str] = field(default_factory=list[str])
    pending: list[str] = field(default_factory=list[str])
    settled: list[str] = field(default_factory=list[str])
    progress: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "collected": self.collected,
            "left": self.left,
            "written": self.written,
            "pending": self.pending,
            "settled": self.settled,
            "progress": self.progress,
        }


def day_start(day: date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min).astimezone()


def _raw_status(source: dict[str, Any]) -> str:
    """A source's status with today's open-day rule taken back out."""
    status = str(source.get("status"))
    return "complete" if status == "partial" and source.get("reason") == DAY_OPEN else status


def _settled(meta: dict[str, Any]) -> bool:
    sources = as_dict(meta.get("sources"))
    return meta.get("status") == "complete" and all(
        as_dict(sources.get(name)).get("status") == "complete" for name in SOURCES
    )


@dataclass
class Marks:
    """Where each source's coverage ends, and the status it stopped with."""

    at: dict[str, dt.datetime]
    status: dict[str, str]


def marks_of(checkpoint: dict[str, Any] | None) -> Marks | None:
    if checkpoint is None or checkpoint_problems(checkpoint):
        return None
    sources = as_dict(checkpoint.get("sources"))
    out = Marks({}, {})
    for name in SOURCES:
        source = as_dict(sources.get(name))
        mark = stamp(source.get("covered_through"))
        if mark is None:
            return None
        out.at[name] = mark.astimezone()
        out.status[name] = str(source.get("status"))
    return out


def plan(where: Path, tag: str, today: date) -> tuple[list[date], Marks]:
    """Every day to collect, oldest first, and the marks the pass starts from: each stopped
    source's own day, and every day from the earliest mark of the sources still moving."""
    marks = marks_of(own_checkpoint(where, tag))
    if marks is None:
        start = day_start(today - dt.timedelta(days=BACKFILL_DAYS))
        marks = Marks({name: start for name in SOURCES}, dict.fromkeys(SOURCES, "complete"))
    moving = [marks.at[name] for name in SOURCES if marks.status[name] == "complete"]
    days = {min(marks.at[name].date(), today) for name in SOURCES if marks.status[name] != "complete"}
    if moving:
        first = min(min(moving).date(), today)
        days |= {first + dt.timedelta(days=n) for n in range((today - first).days + 1)}
    first = min(days) if days else today
    days |= {day for day, meta in own_records(where, tag).items() if day < first and not _settled(meta)}
    return sorted(days), marks


def _active(record: dict[str, Any]) -> bool:
    if any(_raw_status(as_dict(s)) != "complete" for s in as_dict(record.get("sources")).values()):
        return True
    counts = ("requests", "dispatches", "manager_usd", "dispatch_usd", "probe_usd")
    return any(record.get(key) for key in counts)


def _advance(marks: Marks, stopped: set[str], day: date, record: dict[str, Any]) -> None:
    sources = as_dict(record.get("sources"))
    collected = stamp(record.get("updated")) or core.now()
    for name in SOURCES:
        if name in stopped or not day_start(day) <= marks.at[name] < day_end(day):
            continue
        status = _raw_status(as_dict(sources.get(name)))
        marks.status[name] = status
        if status == "complete":
            marks.at[name] = min(day_end(day), collected.astimezone())
        else:
            stopped.add(name)


def checkpoint(tag: str, marks: Marks) -> dict[str, Any]:
    return {
        "tags": [CHECKPOINT_TAG],
        "schema": SCHEMA,
        "machine": tag,
        "updated": core.iso(core.now()),
        "covered_through": core.iso(min(marks.at.values())),
        "sources": {
            name: {"covered_through": core.iso(marks.at[name]), "status": marks.status[name]}
            for name in SOURCES
        },
    }


def _whole_days(mark: dt.datetime) -> date:
    """The last day a mark covers in full. Today's mark moving within the day is not progress."""
    return mark.date() - dt.timedelta(days=1)


def _reason(records: dict[date, dict[str, Any]], day: date, name: str) -> str | None:
    record = records.get(day)
    return None if record is None else str(as_dict(as_dict(record.get("sources")).get(name)).get("reason"))


def owed(
    before: Marks,
    after: Marks,
    left: list[str],
    earlier: dict[date, dict[str, Any]],
    now: dict[date, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """`(pending, settled)`: the work still owed after a pass, and the `unavailable` sources whose
    evidence is unchanged since the pass began: the same status, mark and `reason` on the day
    the mark stops at, in the record published before the pass (`earlier`) and the one just
    collected (`now`). With either record missing the evidence is not known to be unchanged."""
    pending = [f"{len(left)} day(s) not reached, from {left[0]}"] if left else []
    settled: list[str] = []
    for name in SOURCES:
        status, mark = after.status[name], core.iso(after.at[name])
        if status == "complete":
            continue
        day = after.at[name].date()
        reason = _reason(now, day, name)
        same = (
            before.status[name] == status
            and before.at[name] == after.at[name]
            and reason is not None
            and reason == _reason(earlier, day, name)
        )
        if status == "unavailable" and same:
            settled.append(f"{name} unavailable at {mark}")
        else:
            pending.append(f"{name} {status} at {mark}")
    return pending, settled


def _collect(root: Path, day: date, tag: str) -> dict[str, Any]:
    return collect_day(root, day, machine=tag)


def publish(
    root: Path,
    *,
    budget: float = BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
    collect: Callable[[Path, date, str], dict[str, Any]] | None = None,
) -> Outcome:
    """Collect what is due within `budget` seconds and install it. A source that cannot be read
    comes back in the record as `unavailable`, and a failed install as the outcome's status; an
    exception from the collector or the install propagates to the caller."""
    from ..dispatch import machine as machine_mod
    from ..telemetry.dashboard import capture
    from ..telemetry.summary import summary_rel

    tag = machine_mod.resolve(root).tag
    if tag is None:
        return Outcome("no-label", str(machine_mod.resolve(root).problem), pending=["no machine label"])
    deadline = clock() + budget
    where = base(root)
    days, marks = plan(where, tag, core.today())
    before = Marks(dict(marks.at), dict(marks.status))
    existing = own_records(where, tag)
    stopped: set[str] = set()
    files: dict[str, str] = {}
    collected: dict[date, dict[str, Any]] = {}
    outcome = Outcome("current")
    gather = collect or _collect
    for index, day in enumerate(days):
        if clock() >= deadline:
            outcome.left = [d.isoformat() for d in days[index:]]
            break
        record = gather(root, day, tag)
        if problems := record_problems(record):
            left = [d.isoformat() for d in days[index:]]
            return Outcome("invalid", f"{day}: {problems[0]}", outcome.collected, left, pending=[problems[0]])
        outcome.collected.append(day.isoformat())
        collected[day] = record
        _advance(marks, stopped, day, record)
        if _active(record) or day in existing:
            files[record_rel(day, tag)] = render(record)
        from ..telemetry.recorder import main_of

        events = core.agent_path(main_of(root), "telemetry", "events", f"{day.isoformat()}.jsonl")
        if _active(record) or day in existing or day == core.today() or events.is_file():
            files[summary_rel(day, tag)] = json.dumps(capture(root, day, record), indent=2) + "\n"
    mark = checkpoint(tag, marks)
    if problems := checkpoint_problems(mark):
        return Outcome(
            "invalid", f"checkpoint: {problems[0]}", outcome.collected, outcome.left, pending=[problems[0]]
        )
    files[checkpoint_rel(tag)] = render(mark)
    # At the publication boundary refresh collection times too: consumers need a true age
    # even when the figures have stayed still. Local Stop writes never enter this transaction.
    outcome.written = sorted(files)
    outcome.pending, outcome.settled = owed(before, marks, outcome.left, existing, collected)
    days_text = ", ".join(outcome.collected[-3:]) or "none"
    if files:
        result = install(
            root, files, f"usage: {tag}", f"Collected {len(outcome.collected)} day(s), last {days_text}."
        )
        outcome.status, outcome.detail = result.status, result.detail
    else:
        result = None
        outcome.status, outcome.detail = "current", "figures unchanged"
    moved = any(
        _whole_days(marks.at[n]) > _whole_days(before.at[n]) or marks.status[n] != before.status[n]
        for n in SOURCES
    )
    outcome.progress = (result is None or result.status in OK) and moved
    return outcome
