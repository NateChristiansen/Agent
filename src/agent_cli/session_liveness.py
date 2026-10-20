"""Use one liveness rule for manager records: a proven pid follows its process; an unknown pid follows
activity for leases.IDLE_SECONDS, then becomes unverified. Shared Codex daemons cannot prove a
thread pid, so null pid does not mean the session ended.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import core
from .records.narrow import dig


def pid_unknown(record: dict[str, Any]) -> bool:
    """Identity recorded the pid as unknown on purpose, rather than an old record with none."""
    return record.get("pid") is None and bool(record.get("pid_unknown"))


def last_seen(record: dict[str, Any]) -> str | None:
    """The newest valid stamp of `activity.at` (the last state change) and `activity.seen_at` (the activity hook's heartbeat on an unchanged state), or None. A malformed stamp is
    ignored: `minutes_ago` reads one as zero, which kept a dead session listed."""
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
    """No activity for `leases.IDLE_SECONDS`."""
    from . import leases

    activity = record.get("activity")
    if activity and not isinstance(activity, dict):
        return False
    at = last_seen(record)
    return not at or leases.minutes_ago(at) * 60 >= leases.IDLE_SECONDS


def record_live(record: dict[str, Any] | None, alive: Callable[[Any], bool] | None = None) -> bool:
    """A proven pid that is alive, or an unknown pid with recent activity. `alive` is the
    caller's own `is_alive`, so a test that patches it in that module still reaches here."""
    if not record:
        return False
    if pid_unknown(record):
        # An ended session is gone however recent its last event; a known pid cannot say so
        # once its process exits, but an unknown one has only this.
        return not quiet(record) and dig(record, "activity", "state") != "ended"
    return (alive or core.is_alive)(record.get("pid"))


def set_identity(root: Path, session_id: str, pid: int | None, known: bool) -> dict[str, Any]:
    """Write the resolved identity. A known pid clears `pid_unknown`; an unknown one sets
    `pid: null` explicitly, because `set_record` treats a None pid as "keep the old one" and
    would leave the daemon's pid in place."""
    from . import sessions

    def change(record: dict[str, Any]) -> dict[str, Any]:
        if known and pid is not None:
            return {**{k: v for k, v in record.items() if k != "pid_unknown"}, "pid": pid}
        return {**record, "pid": None, "pid_unknown": True}

    return sessions.update_record(root, session_id, change)
