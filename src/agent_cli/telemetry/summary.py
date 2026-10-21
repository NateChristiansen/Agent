"""Fixed telemetry queries and small daily machine records, read without a live probe.

Counts exclude the sink's exclusion manifest and deduplicate event ids. Completeness describes
observed coverage only: a missing day, rejected line, loss or writer gap makes it false. Silent
losses remain unknowable (see ADR-0038). Daily records travel with usage, never raw event lines.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import core
from ..records.narrow import as_dict, as_int, as_list
from . import exclusions, schema
from .health import NOTE, read_health
from .recorder import main_of
from .sink import Sink

FRESH_S = 15 * 60


def summary_rel(day: date, machine: str) -> str:
    return f"usage/telemetry/{day.isoformat()}.{machine}.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}


def window(day: str | None, since: str | None, until: str | None, now: datetime) -> tuple[datetime, datetime]:
    if day and (since or until):
        raise core.AgentError("choose a day or a since/until window")
    if since or until:
        start, end = schema.parse_ts(since), schema.parse_ts(until) if until else now
        if start is None or end is None or start >= end:
            raise core.AgentError("since/until need ordered timestamps with UTC offsets")
    else:
        try:
            chosen = date.fromisoformat(day) if day else now.astimezone().date()
        except ValueError:
            raise core.AgentError("date needs YYYY-MM-DD") from None
        start = datetime.combine(chosen, datetime.min.time()).astimezone()
        end = datetime.combine(chosen + timedelta(days=1), datetime.min.time()).astimezone()
    return start, end


def event_rows(sink: Sink, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], bool]:
    excluded = exclusions.load(sink)
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    complete = True
    day = start.astimezone(UTC).date()
    last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
    while day <= last:
        path = sink.events / f"{day.isoformat()}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeError:
            lines, complete = [], False
        for line in lines:
            try:
                event = as_dict(json.loads(line))
            except ValueError:
                complete = False
                continue
            if schema.validate(event) is not None:
                complete = False
                continue
            at = schema.parse_ts(event["ts"])
            if at is None or not start <= at < end or excluded.skip(event):
                continue
            event_id = str(event["event_id"])
            if event_id not in seen:
                rows.append(event)
                seen.add(event_id)
        day += timedelta(days=1)
    return sorted(rows, key=lambda r: str(r["ts"])), complete


def metric_rows(path: Path, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], bool]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], False
    except OSError, UnicodeError:
        return [], False
    rows: list[dict[str, Any]] = []
    complete = True
    for line in lines:
        try:
            row = as_dict(json.loads(line))
            value = row.get("at")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                complete = False
                continue
            at = datetime.fromtimestamp(value, UTC)
        except ValueError, OverflowError:
            complete = False
            continue
        if start <= at < end:
            rows.append(row)
    return rows, complete


def hand_episodes(
    events: list[dict[str, Any]], paints: list[dict[str, Any]], start: datetime
) -> tuple[dict[str, dict[str, int]], bool]:
    """Count the first visible hand per tab, cause and wait across both painters.

    History before the window prevents a repaint of an older wait becoming a new episode.
    Stale hands are separate from definitive permission/question hands. Unattributed hands
    stay unknown, never inferred to be permissions from a label alone.
    """
    from ..tab_drain import drain_key

    timeline: list[tuple[float, int, dict[str, Any]]] = []
    for event in events:
        if (
            event["kind"]
            in (
                "activity.transition",
                "permission.request",
                "workflow.owner_question",
                "workflow.owner_question_cleared",
            )
            and (at := schema.parse_ts(event["ts"])) is not None
        ):
            timeline.append((at.timestamp(), 0, event))
    for row in paints:
        for paint in as_list(row.get("painted_labels", row.get("painted"))):
            timeline.append((float(row["at"]), 1, as_dict(paint)))
    questions: dict[str, str] = {}
    waits: dict[str, tuple[str, str]] = {}
    runs: dict[str, str] = {}
    seen: set[tuple[str, str, str]] = set()
    counts: dict[str, Counter[str]] = {
        cause: Counter() for cause in ("permission", "question", "stale", "unknown")
    }
    complete = True
    for at, is_paint, row in sorted(timeline, key=lambda item: item[:2]):
        if not is_paint:
            sid, kind = drain_key("manager", str(row.get("session") or "")), row["kind"]
            data = as_dict(row["data"])
            identity = str(data.get("question_id") or row["event_id"])
            if kind == "workflow.owner_question":
                questions[sid] = identity
            elif kind == "workflow.owner_question_cleared":
                if questions.get(sid) == identity:
                    questions.pop(sid)
            elif kind == "permission.request":
                waits[sid] = ("permission", str(row["event_id"]))
            elif data.get("to") != "waiting_on_you":
                waits.pop(sid, None)
            elif str(data.get("cause") or "").startswith("PermissionRequest"):
                waits.setdefault(sid, ("permission", str(row["event_id"])))
            else:
                waits[sid] = ("question", str(row["event_id"]))
            continue
        label = str(row.get("label") or "")
        key = str(row.get("key") or f"unknown-{label.partition(' ')[2]}")
        if not label.startswith("✋"):
            runs[key] = str(at)
            continue
        sid = key
        cause, wait = (
            ("question", questions[sid])
            if sid in questions
            else waits.get(sid, ("unknown", runs.get(key, "initial")))
        )
        if label.startswith("✋?"):
            cause = "stale"
        episode = (key, cause, wait)
        if episode in seen:
            continue
        seen.add(episode)
        if at < start.timestamp():
            continue
        if not row.get("key") or cause == "unknown":
            complete = False
        provider = str(
            row.get("provider")
            or ("codex" if " · cx " in label else "claude" if " · cl " in label else "unknown")
        )
        counts[cause][provider] += 1
    return {cause: dict(count) for cause, count in counts.items()}, complete


def queries(root: Path, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
    sink = Sink.for_main(main_of(root))
    stop = min(end, now + timedelta(microseconds=1))
    rows, events_complete = event_rows(sink, start, stop)
    kinds = Counter(str(r["kind"]) for r in rows)
    losses = read_health(sink, start, stop - timedelta(microseconds=1))["counts"]
    complete = events_complete and not any(losses.values())
    hook_errors = [r for r in rows if r["kind"] == "hook.error"]
    transitions = Counter(
        str(r.get("provider") or "unknown")
        for r in rows
        if r["kind"] == "activity.transition" and as_dict(r["data"]).get("to") == "waiting_on_you"
    )
    requests = Counter(str(r.get("provider") or "unknown") for r in rows if r["kind"] == "permission.request")
    # The two painters have different records. Count successful label writes, not requests.
    drain, drain_complete = metric_rows(core.agent_path(root, "tabs", "metrics.jsonl"), start, stop)
    plugin, plugin_complete = metric_rows(core.agent_path(root, "tabs", "plugin-metrics.jsonl"), start, stop)
    pane, pane_complete = metric_rows(core.agent_path(root, "tabs", "pane-metrics.jsonl"), start, stop)
    complete = complete and drain_complete and plugin_complete
    paints: Counter[str] = Counter()
    for row in [*drain, *plugin]:
        for paint in as_list(row.get("painted_labels", row.get("painted"))):
            label = str(as_dict(paint).get("label") or "")
            if label.startswith("✋"):
                provider = str(
                    as_dict(paint).get("provider")
                    or ("codex" if " · cx " in label else "claude" if " · cl " in label else "unknown")
                )
                paints[provider] += 1
    by_phase: dict[str, float] = {}
    open_by_phase: Counter[str] = Counter()
    spans = 0
    phases: dict[tuple[str, int], list[dict[str, Any]]] = {}
    # A wait can cross several quiet days. Read the preceding history and clip it to the window.
    days: list[date] = []
    for path in sink.events.glob("*.jsonl"):
        try:
            days.append(date.fromisoformat(path.stem))
        except ValueError:
            continue
    oldest = datetime.combine(min(days), datetime.min.time(), UTC) if days else start
    earlier, history_complete = event_rows(sink, oldest, stop)
    history_start = datetime.min.replace(tzinfo=UTC)
    drain_history, _ = metric_rows(core.agent_path(root, "tabs", "metrics.jsonl"), history_start, stop)
    plugin_history, _ = metric_rows(
        core.agent_path(root, "tabs", "plugin-metrics.jsonl"), history_start, stop
    )
    episodes, episodes_complete = hand_episodes(earlier, [*drain_history, *plugin_history], start)
    for row in earlier:
        if row["kind"] == "dispatch.transition":
            phases.setdefault((str(row["dispatch"]), as_int(as_dict(row["data"]).get("attempt"))), []).append(
                row
            )
    for sequence in phases.values():
        for index, row in enumerate(sequence):
            phase = str(as_dict(row["data"]).get("phase"))
            if phase in ("ended", "landed"):
                continue
            left = schema.parse_ts(row["ts"])
            right = schema.parse_ts(sequence[index + 1]["ts"]) if index + 1 < len(sequence) else min(end, now)
            if left is not None and right is not None and min(right, end, now) > max(left, start):
                by_phase[phase] = (
                    by_phase.get(phase, 0.0) + (min(right, end, now) - max(left, start)).total_seconds()
                )
                spans += 1
                if index + 1 == len(sequence):
                    open_by_phase[phase] += 1
    verify: dict[str, Counter[str]] = {}
    flakes = 0
    for row in rows:
        if row["kind"] in ("verify.record", "dispatch.verification", "dispatch.reverify"):
            verify.setdefault(row["kind"], Counter())[str(row.get("outcome") or "unknown")] += 1
            data = as_dict(row["data"])
            flakes += (
                as_int(data.get("retry_count"))
                if "retry_count" in data
                else len(as_list(data.get("retry_nodes")))
            )
    return {
        "complete": complete,
        "owner_questions": {
            "raised": kinds["workflow.owner_question"],
            "cleared": kinds["workflow.owner_question_cleared"],
        },
        "permissions": {
            "requests": dict(requests),
            "raw_waiting_transitions": dict(transitions),
            "visible_hand_paints": dict(paints),
            "hand_episodes": episodes,
            "episodes_complete": episodes_complete
            and history_complete
            and drain_complete
            and plugin_complete
            and complete,
            "paints_complete": drain_complete and plugin_complete,
            "approver": "unknown",
        },
        "dispatch": {
            "launches": kinds["dispatch.launch"],
            "terminal": kinds["dispatch.terminal"],
            "spans": spans,
            "seconds_by_phase": dict(by_phase),
            "seconds_unit": "summed dispatch-seconds; concurrent spans add, each clipped to the window",
            "open_spans": sum(open_by_phase.values()),
            "open_spans_by_phase": dict(open_by_phase),
        },
        "verify": {k: dict(v) for k, v in verify.items()},
        "flakes": {"retried_nodes": flakes},
        "health": {
            "losses": losses,
            "hook_errors": kinds["hook.error"],
            "last_hook_error": {
                "hook": as_dict(hook_errors[-1]["data"]).get("hook"),
                "reason": as_dict(hook_errors[-1]["data"]).get("reason"),
                "at": hook_errors[-1]["ts"],
            }
            if hook_errors
            else None,
            # A metrics file that does not exist yet has nothing to lose; only an unreadable or
            # malformed one leaves the counts in doubt.
            "coverage": {
                "events": events_complete,
                "plugin_metrics": plugin_complete
                or not core.agent_path(root, "tabs", "plugin-metrics.jsonl").exists(),
                "pane_metrics": pane_complete
                or not core.agent_path(root, "tabs", "pane-metrics.jsonl").exists(),
            },
            "render_failures": kinds["status.render"] + sum(as_int(r.get("failed")) for r in pane),
            "tab_paint_failures": sum(as_int(r.get("failed")) for r in plugin),
            "notes_sync": dict(
                Counter(
                    str(as_dict(r["data"]).get("status") or "unknown")
                    for r in rows
                    if r["kind"] == "notes.sync"
                )
            ),
            "note": NOTE,
        },
    }


def summary(
    root: Path,
    *,
    day: str | None = None,
    since: str | None = None,
    until: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The CLI and MCP result. No network, free-form queries or writes."""
    from ..dispatch.machine import resolve

    moment = now or core.now()
    start, end = window(day, since, until, moment)
    tag = resolve(root).tag
    local = queries(root, start, end, moment)
    machines: list[dict[str, Any]] = []
    day_at = start.astimezone().date()
    last_day = (min(end, moment + timedelta(microseconds=1)) - timedelta(microseconds=1)).astimezone().date()
    while day_at <= last_day:
        machines.extend(machine_days(root, day_at, moment))
        day_at += timedelta(days=1)
    return {
        "schema": 1,
        "machine": tag,
        "since": core.iso(start),
        "until": core.iso(end),
        "updated": core.iso(moment),
        "age_s": 0,
        "complete": end <= moment
        and local["complete"]
        and bool(machines)
        and all(m["complete"] for m in machines),
        "local": local,
        "machines": machines,
    }


def machine_days(root: Path, day: date, now: datetime) -> list[dict[str, Any]]:
    from ..dispatch.machine import resolve
    from ..usage.files import base

    where = base(root)
    tags = {p.stem for p in (where / "usage/machines").glob("*.md")}
    tags |= {p.name.split(".")[-2] for p in (where / "usage/telemetry").glob("*.json")}
    if tag := resolve(root).tag:
        tags.add(tag)
    out: list[dict[str, Any]] = []
    for tag in sorted(tags):
        record = read_json(where / summary_rel(day, tag))
        updated = schema.parse_ts(record.get("updated"))
        age = max(0, int((now - updated).total_seconds())) if updated else None
        out.append(
            {
                **record,
                "machine": tag,
                "date": day.isoformat(),
                "updated": record.get("updated"),
                "age_s": age,
                "complete": record.get("complete") is True
                and updated is not None
                and updated <= now
                and record.get("machine") == tag
                and record.get("date") == day.isoformat(),
                "missing": not bool(record),
            }
        )
    return out
