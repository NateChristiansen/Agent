# ruff: noqa
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Any
from .features import Feature
from .records.narrow import as_dict, dig

STATUS_PREFIX = "status "
TICK_NOTE = "ticked while the feature is blocked"
CLEAR_HINT = "clear it with feature_set status=active (and a reason) once the blocker is gone"


def tick_entry(index: int) -> str:
    return f"task #{index} {TICK_NOTE}"


def status_entry(old: str, new: str, reason: str | None) -> str:
    return f"{STATUS_PREFIX}{old} → {new}: {(reason or '').strip() or 'no reason given'}"


def _log_time(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return None


def _iso_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _entries(feature: Feature) -> list[Any]:
    from . import log as log_mod

    try:
        return log_mod.parse(feature.log_path) if feature.log_path.exists() else []
    except Exception:
        return []


def since(feature: Feature, entries: list[Any] | None = None) -> tuple[str, str] | None:
    for entry in reversed(entries if entries is not None else _entries(feature)):
        text = str(entry.text)
        if text.startswith(STATUS_PREFIX) and "→ blocked" in text.splitlines()[0]:
            return (entry.ts, text.partition(": ")[2].strip())
    return None


def active_work(root: Path, feature: Feature, dispatches: list[dict[str, Any]] | None = None) -> list[str]:
    if feature.status != "blocked":
        return []
    try:
        entries = _entries(feature)
        blocked = since(feature, entries)
        start = _log_time(blocked[0]) if blocked else None
        if dispatches is None:
            from .dispatch import state as dispatch_state

            dispatches = dispatch_state.load_all(root)
        out: list[str] = []
        for d in dispatches:
            if d.get("feature") != feature.id:
                continue
            ident, state = (str(d.get("id") or "?"), str(d.get("state") or ""))
            began = _iso_time(d.get("started")) or _iso_time(d.get("created"))
            if state in ("queued", "running"):
                out.append(f"dispatch {ident} {state}")
            elif start and began and (began >= start):
                out.append(f"dispatch {ident} started {d.get('started') or d.get('created')}")
            landed = (
                as_dict(d.get("actioned")).get("action") == "landed"
                or dig(d, "integration", "state") == "landed"
            )
            landed_at = _iso_time(d.get("actioned_at"))
            if start and landed and landed_at and (landed_at >= start):
                out.append(f"landed {ident} at {d.get('actioned_at')}")
        if blocked:
            for e in entries:
                text = str(e.text)
                if TICK_NOTE in text and e.ts >= blocked[0]:
                    out.append(f"{text.partition(' ' + TICK_NOTE)[0]} ticked {e.ts}")
        return out
    except Exception:
        return []


def session_lines(root: Path, feature: Feature) -> list[str]:
    if feature.status != "blocked":
        return []
    blocked = since(feature)
    head = (
        f"feature {feature.number} is BLOCKED since {blocked[0]}: {blocked[1]}"
        if blocked
        else f"feature {feature.number} is BLOCKED (no logged reason)"
    )
    lines = [head]
    work = active_work(root, feature)
    if work:
        lines.append("  active work since: " + "; ".join(work))
    lines.append("  " + CLEAR_HINT)
    return lines
