"""Health facts captured with each daily telemetry summary, and the rows live.json shows.

`capture` records what one machine observed at its Stop: notes sync, the newest peer send, hook
errors and vault display failures, with the telemetry coverage behind the counts. Reads are local
and never probe a network link. `live_health` decides every row: one state (ok, action or
unknown), a headline, what it means and, when the owner must act, what to do. The plugin only lays
the rows out and applies their clock deadlines.

Each machine row is observed at its record's `updated` and stands `FRESH_S`. Past that the plugin
shows the row's `expired` form: a problem stays as unknown history. Another machine's other rows
fold into one "no report" row for it; this machine's each read unknown. An accepted send is delivery history, never proof that the other
machine is reachable now. The lesson queue is one local file, read when the snapshot is built.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import core, notes_sync, tab_plugin
from ..records.narrow import as_dict, as_int
from .schema import parse_ts
from .summary import FRESH_S, machine_days, queries, read_json, summary_rel, window

KEYS = ("notes_sync", "peer_link", "hook_errors", "vault_display")
LABELS = {
    "notes_sync": "Notes sync",
    "peer_link": "Pairing link",
    "hook_errors": "Hooks",
    "vault_display": "Vault display",
    "lessons": "Lessons",
    "machine": "Machine",
}
MEANINGS = {
    "notes_sync": "Whether the machine's work records reach your notes repo.",
    "peer_link": "Whether the last message to the other machine was accepted. "
    "It does not show that the other machine is reachable now.",
    "hook_errors": "Whether harness hooks ran without raising. A hook that raised may have skipped "
    "its work (tab icon, usage, notes sync).",
    "vault_display": "Whether manager tab icons, generated views and this pane render.",
    "lessons": "Findings with no open feature, waiting to be routed. At 10, feature close refuses.",
}
ACTIONS = {
    "notes_sync": "Ask the manager to resolve the sync failure; for a halt, run the clear command it names "
    "(chapter 08).",
    "peer_link": "Run agent peer-net show and check SSH and that receiving is on (chapter 11).",
    "hook_errors": 'Ask the manager: "show today\'s hook errors and what failed".',
    "vault_display": "Reload the Agent Terminal plugin; check Obsidian's CLI is enabled (chapter 14).",
    "lessons": "Run /lesson review in a manager.",
}
#: Sync outcomes that retry by themselves at the next hook; they never ask the owner to act.
SELF_RETRYING = ("offline", "busy")
#: An undelivered peer message is normal retrying until it is this old.
PAIRING_GRACE_S = 10 * 60
#: A last successful notes sync older than this no longer reads as ok (owner, 2026-10-27).
NOTES_STALE_S = 6 * 3600
OPEN_SENDS = ("pending", "sending", "unconfirmed")
STATE_ORDER = {"action": 0, "unknown": 1, "ok": 2}
SOURCES = {"events": "event log", "plugin_metrics": "tab metrics", "pane_metrics": "pane metrics"}


def peer_link(root: Path) -> dict[str, Any]:
    """This install's enrollment and its newest outbox row, read-only. No store means not enrolled."""
    path = core.agent_path(root, "peer-net", "state.db")
    if not path.is_file():
        return {"enrolled": False}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.05)
        try:
            device = conn.execute("SELECT v FROM enrollment WHERE k='device'").fetchone()
            row = conn.execute(
                "SELECT state, updated_at, alias FROM outbox ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return {"enrolled": True, "readable": False}
    if device is None:
        return {"enrolled": False}
    if not row:
        return {"enrolled": True, "readable": True, "state": None}
    state, at, alias = row
    return {
        "enrolled": True,
        "readable": True,
        "state": str(state),
        "at": datetime.fromtimestamp(at, UTC).isoformat(),
        "alias": alias,
    }


def capture(root: Path, day: date, usage: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """One small per-machine day; usage and telemetry share its publication transaction."""
    from .. import config_file

    moment = now or core.now()
    start, end = window(day.isoformat(), None, None, moment)
    local = queries(root, start, end, moment)
    complete = local["complete"] and usage.get("status") == "complete"
    health = as_dict(local["health"])
    kept = notes_sync.last_outcomes(root)
    attempt = as_dict(kept.get("last_attempt"))
    error = as_dict(kept.get("last_error"))
    ok = as_dict(kept.get("last_ok"))
    beat = tab_plugin.heartbeat(root)
    facts: dict[str, Any] = {
        "notes_sync": {
            "mode": config_file.notes_sync_mode(root),
            "attempt_at": attempt.get("at"),
            "attempt_status": attempt.get("status"),
            "last_ok_at": ok.get("at"),
            "last_error_at": error.get("at"),
            "last_error_status": error.get("status"),
            "last_error": error.get("detail"),
            "error_current": bool(error) and as_int(error.get("seq")) > as_int(ok.get("seq")),
        },
        "peer_link": peer_link(root),
        "hook_errors": {"count": health["hook_errors"], "last": health["last_hook_error"]},
        "vault_display": {
            "tab_paint_failures": health["tab_paint_failures"],
            "render_failures": health["render_failures"],
            "heartbeat_at": beat.get("at_iso"),
            "heartbeat_read_error": beat.get("read_error"),
        },
        "coverage": {**as_dict(health["coverage"]), "losses": health["losses"]},
    }
    return {
        "schema": 2,
        "date": day.isoformat(),
        "machine": usage.get("machine"),
        "updated": core.iso(moment),
        "complete": complete,
        "local": local,
        "health": facts,
    }


def _clock(stamp: Any, moment: datetime) -> str | None:
    """`14:02` for a stamp from today, `Oct 18 14:02` for an older one, in local time."""
    at = parse_ts(stamp)
    if at is None:
        return None
    local = at.astimezone(moment.astimezone().tzinfo)
    return local.strftime("%H:%M" if local.date() == moment.astimezone().date() else "%b %d %H:%M")


def _age(prefix: str, at: Any, suffix: str = " ago") -> list[dict[str, Any]]:
    return [{"prefix": prefix, "at": at, "suffix": suffix}] if parse_ts(at) else []


def _row(
    signal: str,
    machine: str | None,
    state: str,
    headline: str,
    *,
    ages: list[dict[str, Any]] | None = None,
    note: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    title = f"{LABELS[signal]} · {machine}" if machine else LABELS[signal]
    return {
        "key": f"{signal}:{machine or ''}",
        "signal": signal,
        "machine": machine,
        "title": title,
        "group": signal,
        "brief": LABELS[signal],
        "state": state,
        "headline": headline,
        "ages": ages or [],
        "note": note,
        "meaning": MEANINGS.get(signal),
        "action": None if state == "ok" else action or ACTIONS.get(signal),
    }


def _incomplete(coverage: dict[str, Any], *parts: str) -> str | None:
    """Why the counts behind a row cannot be trusted, or None when they can."""
    losses = {k: v for k, v in as_dict(coverage.get("losses")).items() if as_int(v)}
    if losses:
        return (
            "telemetry incomplete ("
            + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in losses.items())
            + ")"
        )
    missing = [SOURCES[p] for p in parts if coverage.get(p) is False]
    return f"telemetry incomplete ({', '.join(missing)} missing or unreadable)" if missing else None


def _notes(fact: dict[str, Any], machine: str, moment: datetime) -> dict[str, Any] | None:
    if fact.get("mode") == "off":
        return None
    if not fact.get("attempt_at"):
        return _row(
            "notes_sync",
            machine,
            "unknown",
            "no sync recorded",
            action="Ask the manager to check notes sync with agent doctor.",
        )
    last_sync = _age(" · last sync ", fact.get("last_ok_at"))
    if fact.get("error_current"):
        status = str(fact.get("last_error_status") or "error")
        since = _clock(fact.get("last_error_at"), moment)
        headline = f"{status} since {since}" if since else status
        detail = str(fact.get("last_error") or "")
        if status in SELF_RETRYING:
            return _row(
                "notes_sync",
                machine,
                "unknown",
                f"{headline}; retries by itself",
                ages=last_sync,
                note=detail or None,
                action="Nothing; it retries at the next hook. Check the network if it stays offline.",
            )
        return _row("notes_sync", machine, "action", headline, ages=last_sync, note=detail or None)
    if not fact.get("last_ok_at"):
        status = str(fact.get("attempt_status") or "attempted")
        return _row(
            "notes_sync",
            machine,
            "unknown",
            f"{status}; no successful sync recorded",
            action="Ask the manager to check notes sync with agent doctor.",
        )
    synced = parse_ts(fact.get("last_ok_at"))
    stale = _row(
        "notes_sync",
        machine,
        "unknown",
        f"no successful sync in the last {NOTES_STALE_S // 3600}h",
        ages=last_sync,
        action="Nothing, if no manager ran here. Otherwise ask the manager to check notes sync "
        "with agent doctor.",
    )
    if synced and (moment - synced).total_seconds() > NOTES_STALE_S:
        return stale
    row = _row("notes_sync", machine, "ok", str(fact.get("attempt_status") or "current"), ages=last_sync)
    if synced:
        # an open pane turns the row stale on the clock, without a new snapshot
        row["stale_at"] = core.iso(synced + timedelta(seconds=NOTES_STALE_S))
        row["stale"] = {key: stale[key] for key in ("state", "headline", "action")}
    return row


def _pairing(fact: dict[str, Any], machine: str, observed: datetime | None) -> dict[str, Any] | None:
    if not fact.get("enrolled"):
        return None
    if not fact.get("readable"):
        return _row("peer_link", machine, "unknown", "send history unreadable")
    state = fact.get("state")
    if state is None:
        return _row("peer_link", machine, "ok", "no message sent yet")
    to = f" to {fact['alias']}" if fact.get("alias") else ""
    sent = parse_ts(fact.get("at"))
    if state == "accepted":
        return _row("peer_link", machine, "ok", f"last message{to} accepted", ages=_age(" ", fact.get("at")))
    if state in OPEN_SENDS:
        waited = (observed - sent).total_seconds() if observed and sent else None
        if waited is not None and waited < PAIRING_GRACE_S:
            return _row(
                "peer_link", machine, "ok", f"message{to} sending", ages=_age(", queued ", fact.get("at"))
            )
        return _row(
            "peer_link",
            machine,
            "action",
            f"message{to} not delivered",
            ages=_age(", queued ", fact.get("at")),
        )
    return _row("peer_link", machine, "action", f"last message{to} {state}", ages=_age(" ", fact.get("at")))


def _hooks(fact: dict[str, Any], coverage: dict[str, Any], machine: str, moment: datetime) -> dict[str, Any]:
    count = as_int(fact.get("count"))
    if count:
        last = as_dict(fact.get("last"))
        hook = last.get("hook") or "unknown hook"
        when = _clock(last.get("at"), moment)
        reason = f" ({last['reason']})" if last.get("reason") else ""
        noun = "error" if count == 1 else "errors"
        return _row(
            "hook_errors",
            machine,
            "action",
            f"{count} {noun} today · last: {hook}{reason}" + (f", {when}" if when else ""),
        )
    if why := _incomplete(coverage, "events"):
        return _row(
            "hook_errors",
            machine,
            "unknown",
            f"no errors counted, but {why}",
            action="Ask the manager to run agent doctor and explain the telemetry losses.",
        )
    return _row("hook_errors", machine, "ok", "no errors today")


def _display(
    fact: dict[str, Any], coverage: dict[str, Any], machine: str, observed: datetime | None
) -> dict[str, Any]:
    paint = as_int(fact.get("tab_paint_failures"))
    render = as_int(fact.get("render_failures"))
    problems = [
        *([f"{paint} tab paint failure{'s' * (paint != 1)}"] if paint else []),
        *([f"{render} render failure{'s' * (render != 1)}"] if render else []),
        *(["plugin heartbeat unreadable"] if fact.get("heartbeat_read_error") else []),
    ]
    if problems:
        return _row("vault_display", machine, "action", " · ".join(problems) + " today")
    beat = parse_ts(fact.get("heartbeat_at"))
    if beat is None or (observed is not None and (observed - beat).total_seconds() > FRESH_S):
        return _row(
            "vault_display",
            machine,
            "unknown",
            "the Agent Terminal plugin has not reported" + (" since" if beat else ""),
            ages=_age(" ", fact.get("heartbeat_at")),
            action="Open Obsidian with the Agent Terminal plugin enabled (chapter 14).",
        )
    if why := _incomplete(coverage, "events", "plugin_metrics", "pane_metrics"):
        row = _row(
            "vault_display",
            machine,
            "unknown",
            f"no failures counted, but {why}",
            action="Ask the manager to run agent doctor and explain the telemetry losses.",
        )
    else:
        row = _row("vault_display", machine, "ok", "tabs and views painting")
    # the painting is only as fresh as the plugin's heartbeat, however recent the capture
    if observed is not None and (observed - beat).total_seconds() > tab_plugin.HEARTBEAT_STALE_S:
        row["observed"] = fact.get("heartbeat_at")
    return row


def _legacy(key: str, items: list[dict[str, Any]], machine: str) -> dict[str, Any] | None:
    """A row from a record published before these facts existed: the worst of its statuses."""
    items = [item for item in items if item]
    if not items:
        return None
    states = [
        {"ok": "ok", "warn": "action", "fail": "action"}.get(str(i.get("status")), "unknown") for i in items
    ]
    state = min(states, key=STATE_ORDER.__getitem__)
    detail = " · ".join(str(i.get("detail") or "reported by an older harness") for i in items)
    return _row(key, machine, state, detail)


def machine_rows(record: dict[str, Any], moment: datetime, *, local: bool = False) -> list[dict[str, Any]]:
    """One machine's rows, each standing `FRESH_S` from the record's observation. Another
    machine's then fold into its "no report" row, which the plugin shows once they expire, and
    only its problems stay. This machine's rows each turn unknown instead; it gets the one line
    only when it has no record today."""
    machine = str(record.get("machine") or "unknown")
    health = as_dict(record.get("health"))
    observed = parse_ts(record.get("updated"))
    rows: list[dict[str, Any]] = []
    if record.get("schema") == 2:
        coverage = as_dict(health.get("coverage"))
        for row in (
            _notes(as_dict(health.get("notes_sync")), machine, moment),
            _pairing(as_dict(health.get("peer_link")), machine, observed),
            _hooks(as_dict(health.get("hook_errors")), coverage, machine, moment),
            _display(as_dict(health.get("vault_display")), coverage, machine, observed),
        ):
            if row is not None:
                rows.append(row)
    else:
        for key in KEYS:
            parts = ("tab_paint_failures", "render_failures") if key == "vault_display" else (key,)
            row = _legacy(key, [as_dict(health.get(part)) for part in parts], machine)
            if row is not None:
                rows.append(row)
    until = core.iso(observed + timedelta(seconds=FRESH_S)) if observed else None
    for row in rows:
        stamp = row.pop("observed", None) or record.get("updated")
        at = parse_ts(stamp)
        row["updated"] = stamp
        row["valid_until"] = core.iso(at + timedelta(seconds=FRESH_S)) if at else None
        if row["state"] != "ok":
            row["expired"] = {
                "state": "unknown",
                "headline": f"last report: {row['headline']}",
                "action": f"Open a manager on {machine} to refresh this report; if the problem "
                f"remains: {row['action']}",
            }
        elif local or row["valid_until"] != until:
            # an ok row observed before the report itself expires alone, never silently
            row["expired"] = {
                "state": "unknown",
                "headline": f"last report: {row['headline']}",
                "ages": [*row["ages"], *_age(" · observed ", stamp)],
                "action": "The next turn end in a manager on this machine reports it again."
                if local
                else f"Open a manager on {machine} so it reports again.",
            }
        else:
            row["expired"] = None
        stale_at, stale = parse_ts(row.pop("stale_at", None)), row.pop("stale", None)
        deadline = parse_ts(row["valid_until"])
        if stale_at and (deadline is None or stale_at < deadline):
            row["valid_until"], row["expired"] = core.iso(stale_at), stale
    if local and observed:
        return rows
    problems = sum(row["state"] != "ok" for row in rows)
    silent = _row(
        "machine",
        machine,
        "unknown",
        "no report" if observed else "no report today",
        ages=_age(" for ", record.get("updated"), ""),
        note="last report had no problems"
        if observed and not problems
        else f"last report had {problems} problem{'s' * (problems != 1)}"
        if observed
        else None,
        action=f"Nothing, if {machine} is off. Otherwise open a manager there so it reports.",
    )
    silent["title"] = silent["brief"] = machine
    span = f"in the last {FRESH_S // 60} minutes" if observed else "today"
    silent["meaning"] = f"Whether {machine} has sent a health report {span}."
    silent["updated"] = record.get("updated")
    silent["show_after"] = until
    silent["valid_until"] = None
    silent["expired"] = None
    return [*rows, silent]


def lesson_row(root: Path, stamp: dict[str, Any]) -> dict[str, Any]:
    """`lessons: N open` from the local queue; at `NUDGE_THRESHOLD` it needs review. A missing
    queue is empty; one that cannot be read is unknown, never zero."""
    from .. import lessons

    try:
        count: int | None = len(lessons.open_entries(root))
    except Exception:  # noqa: BLE001 — an unreadable queue is shown, not raised
        count = None
    if count is None:
        row = _row("lessons", None, "unknown", "lessons: unknown — queue unreadable")
        row["action"] = f"Check that {lessons.QUEUE} in your notes repo can be read."
    elif count >= lessons.NUDGE_THRESHOLD:
        row = _row("lessons", None, "action", f"lessons: {count} open — review needed")
    else:
        row = _row("lessons", None, "ok", f"lessons: {count} open")
    row["count"] = count
    row["brief"] = row["headline"]
    row["updated"] = None
    row["observed_at"] = stamp.get("observed_at")
    row["valid_until"] = stamp.get("valid_until")
    row["expired"] = {
        "state": "unknown",
        "headline": "lessons: unknown — snapshot stale",
        "action": "Wait for the next snapshot, or run agent status render.",
    }
    return row


def live_health(
    root: Path, *, now: datetime | None = None, stamp: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Every machine's latest record (this machine's from its local Stop record) and the lesson
    queue, as rows ordered action, unknown, ok. `stamp` carries the snapshot's own observation
    window for the lesson row."""
    from ..dispatch.machine import resolve

    moment = now or core.now()
    day = moment.astimezone().date()
    records = machine_days(root, day, moment)
    if tag := resolve(root).tag:
        local = read_json(core.agent_path(root, "usage") / summary_rel(day, tag).removeprefix("usage/"))
        if local:
            records = [r for r in records if r["machine"] != tag] + [{**local, "machine": tag}]
    rows = [
        row
        for record in records
        for row in machine_rows(record, moment, local=tag is not None and record.get("machine") == tag)
    ]
    rows.append(lesson_row(root, stamp or {}))
    for row in rows:
        row["order"] = [STATE_ORDER[row["state"]], list(LABELS).index(row["signal"]), row["machine"] or ""]
    return {"schema": 2, "rows": sorted(rows, key=lambda row: row["order"])}
