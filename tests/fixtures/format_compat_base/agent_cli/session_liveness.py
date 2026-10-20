# ruff: noqa
from __future__ import annotations
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from . import core
from .records.narrow import dig


def pid_unknown(record: dict[str, Any]) -> bool:
    return record.get("pid") is None and bool(record.get("pid_unknown"))


def last_seen(record: dict[str, Any]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for value in (dig(record, "activity", "at"), dig(record, "activity", "seen_at")):
        if not isinstance(value, str) or not value:
            continue
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            continue
        parsed.append((stamp if stamp.tzinfo else stamp.astimezone(), value))
    return max(parsed)[1] if parsed else None


def quiet(record: dict[str, Any]) -> bool:
    from . import leases

    activity = record.get("activity")
    if activity and (not isinstance(activity, dict)):
        return False
    at = last_seen(record)
    return not at or leases.minutes_ago(at) * 60 >= leases.IDLE_SECONDS


def record_live(record: dict[str, Any] | None, alive: Callable[[Any], bool] | None = None) -> bool:
    if not record:
        return False
    if pid_unknown(record):
        return not quiet(record) and dig(record, "activity", "state") != "ended"
    return (alive or core.is_alive)(record.get("pid"))


def set_identity(root: Path, session_id: str, pid: int | None, known: bool) -> dict[str, Any]:
    from . import sessions

    def change(record: dict[str, Any]) -> dict[str, Any]:
        if known and pid is not None:
            return {**{k: v for k, v in record.items() if k != "pid_unknown"}, "pid": pid}
        return {**record, "pid": None, "pid_unknown": True}

    return sessions.update_record(root, session_id, change)
