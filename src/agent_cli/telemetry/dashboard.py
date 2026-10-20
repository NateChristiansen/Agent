"""Health signals captured with each daily telemetry summary and projected into live.json.

Reads are local and never probe a network link. A peer link reflects its latest recorded send,
not enrollment. Signals keep their observation time; fresh partial records cannot report ok.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import core, notes_sync, tab_plugin
from ..records.narrow import as_dict, as_float, as_int
from .summary import FRESH_S, machine_days, queries, read_json, summary_rel, window

KEYS = (
    "notes_sync",
    "peer_link",
    "verify_queue",
    "spend_today",
    "hook_errors",
    "tab_paint_failures",
    "render_failures",
    "telemetry",
)


def signal(status: str, updated: Any, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "updated": updated, "detail": detail, **extra}


def peer_link(root: Path) -> dict[str, Any]:
    path = core.agent_path(root, "peer-net", "state.db")
    if not path.is_file():
        return signal("unknown", None, "no recorded send")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.05)
        try:
            row = conn.execute(
                "SELECT state, updated_at FROM outbox ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return signal("unknown", None, "send history unreadable")
    if not row:
        return signal("unknown", None, "no recorded send")
    state, at = row
    status = "ok" if state == "accepted" else "warn" if state in ("pending", "sending") else "fail"
    return signal(status, datetime.fromtimestamp(at, UTC).isoformat(), f"last send {state}")


def capture(root: Path, day: date, usage: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """One small per-machine day; usage and telemetry share its publication transaction."""
    moment = now or core.now()
    start, end = window(day.isoformat(), None, None, moment)
    local = queries(root, start, end, moment)
    complete = local["complete"] and usage.get("status") == "complete"
    updated = core.iso(moment)
    health = as_dict(local["health"])
    kept = notes_sync.last_outcomes(root)
    attempt = as_dict(kept.get("last_attempt"))
    error = as_dict(kept.get("last_error"))
    ok = as_dict(kept.get("last_ok"))
    failed = bool(error) and as_int(error.get("seq")) > as_int(ok.get("seq"))
    notes = signal(
        "fail" if failed else "ok" if ok else "unknown",
        attempt.get("at"),
        str(attempt.get("status") or "no sync recorded"),
        last_error=error.get("detail"),
        last_error_at=error.get("at"),
        last_ok_at=ok.get("at"),
    )
    if error:
        notes["detail"] += f"; last error: {error.get('detail') or error.get('status')}"
    waiting: list[dict[str, Any]] = []
    for path in core.agent_path(root, "dispatches").glob("*.json"):
        record = read_json(path)
        released = as_dict(record.get("slot_released"))
        if (
            record.get("state") == "running"
            and released.get("attempt") == record.get("attempt")
            and record.get("worker_cleanup_confirmed")
            and record.get("verification") is None
            and core.is_alive(record.get("supervisor_pid"))
        ):
            waiting.append(record)
    beat = tab_plugin.heartbeat(root)
    spend_complete = usage.get("spend_complete") is True and usage.get("manager_complete") is True
    cost_keys = ("manager_usd", "dispatch_usd", "probe_usd")
    has_costs = any(key in usage for key in cost_keys)
    signals = {
        "notes_sync": notes,
        "peer_link": peer_link(root),
        "verify_queue": signal(
            "warn" if waiting else "ok", updated, f"{len(waiting)} waiting", count=len(waiting)
        ),
        "spend_today": signal(
            "ok" if spend_complete else "warn" if has_costs else "unknown",
            usage.get("updated"),
            f"${sum(as_float(usage.get(k)) for k in cost_keys):.2f}" if has_costs else "unavailable",
            complete=spend_complete,
        ),
        "hook_errors": signal("fail" if health["hook_errors"] else "ok", updated, str(health["hook_errors"])),
        "tab_paint_failures": signal(
            "fail" if health["tab_paint_failures"] or beat.get("read_error") else "ok" if beat else "unknown",
            beat.get("at_iso"),
            str(health["tab_paint_failures"]),
        ),
        "render_failures": signal(
            "fail" if health["render_failures"] else "ok", updated, str(health["render_failures"])
        ),
        "telemetry": signal(
            "ok" if local["complete"] else "warn", updated, "observed coverage", complete=complete
        ),
    }
    for item in signals.values():
        item.setdefault("complete", local["complete"])
    return {
        "schema": 1,
        "date": day.isoformat(),
        "machine": usage.get("machine"),
        "updated": updated,
        "complete": complete,
        "local": local,
        "health": signals,
    }


def live_health(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Published machine summaries plus this machine's latest local Stop record."""
    from ..dispatch.machine import resolve

    moment = now or core.now()
    day = moment.astimezone().date()
    records = machine_days(root, day, moment)
    if tag := resolve(root).tag:
        local = read_json(core.agent_path(root, "usage") / summary_rel(day, tag).removeprefix("usage/"))
        if local:
            records = [r for r in records if r["machine"] != tag] + [local]
    out: dict[str, Any] = {key: [] for key in KEYS}
    for record in records:
        for key in KEYS:
            item = as_dict(as_dict(record.get("health")).get(key))
            updated = item.get("updated") or record.get("updated")
            from .schema import parse_ts

            at = parse_ts(updated)
            valid = core.iso(at + timedelta(seconds=FRESH_S)) if at else None
            complete = item.get("complete", record.get("complete")) is True
            status = item.get("status") or "unknown"
            if key == "notes_sync" and item.get("detail") == "no sync recorded":
                status = "unknown"
            if status == "ok" and not complete:
                status = "warn"
            out[key].append(
                {
                    **item,
                    "machine": record.get("machine"),
                    "updated": updated,
                    "valid_until": valid,
                    "complete": complete,
                    "status": status,
                    "detail": item.get("detail") or "missing machine day",
                }
            )
    return out
