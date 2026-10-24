"""Map sessions to features under `.agent/sessions/`. Re-export session_identity helpers for
compatibility, but patch their owning module: a facade binding does not change a helper looked up
there.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import time
from collections.abc import Callable, Generator
from datetime import timedelta
from pathlib import Path
from typing import Any

from .core import AgentError, agent_parts, agent_path, atomic_write, iso, json_files, now, reads_strict
from .records.narrow import as_dict, as_dict_or_none, as_float, as_float_or_none, as_list
from .session_identity import CLAUDE_ENV_VARS as CLAUDE_ENV_VARS
from .session_identity import CLIENT_HINTS as CLIENT_HINTS
from .session_identity import CODEX_ENV_VARS as CODEX_ENV_VARS
from .session_identity import DEFAULT_PROVIDER as DEFAULT_PROVIDER
from .session_identity import ENV_VARS as ENV_VARS
from .session_identity import candidate_pids as candidate_pids
from .session_identity import client_pid as client_pid
from .session_identity import client_process as client_process
from .session_identity import pid_running_session as pid_running_session
from .session_identity import resolve_identity as resolve_identity
from .session_identity import resolve_pid as resolve_pid
from .session_identity import resolve_provider as resolve_provider
from .session_identity import resolve_session_id as resolve_session_id

try:
    import fcntl
except ImportError:  # Windows uses win_lock instead
    fcntl = None  # type: ignore[assignment]

SESSIONS_DIR = agent_path(Path(), "sessions")


def sessions_dir(root: Path) -> Path:
    return root / SESSIONS_DIR


def _path(root: Path, session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id)
    if not safe:
        raise AgentError("session id is empty")
    return sessions_dir(root) / f"{safe}.json"


BY_PID = "by-pid"


def by_pid_path(root: Path, pid: int | str) -> Path:
    """`.agent/sessions/by-pid/<pid>`: the session id of the Claude process with that pid.
    Written by the session-start hook, read by the MCP server, which has no session id of
    its own but shares a parent process with the hook."""
    return sessions_dir(root) / BY_PID / str(int(pid))


BY_PID_LOCK = ".lock"
BUSY = "session mutation lock is busy; retry after the current operation"


def _file_lock_supported() -> bool:
    return fcntl is not None or os.name == "nt"


@contextlib.contextmanager
def _file_locked(root: Path, parts: tuple[str, ...], timeout: float | None = None) -> Generator[None]:
    """Hold one lock file under `root`. `timeout` bounds the wait; None waits as before."""
    if os.name == "nt":
        from .win_lock import exclusive, open_lock_file

        fd = open_lock_file(root, parts)
        try:
            lock = exclusive(fd) if timeout is None else exclusive(fd, timeout=timeout)
            try:
                lock.__enter__()
            except TimeoutError:
                raise AgentError(BUSY) from None
            try:
                yield
            finally:
                lock.__exit__(None, None, None)
        finally:
            os.close(fd)
        return
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (timeout or 0)
    with open(path, "a") as fh:
        while fcntl is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX if timeout is None else fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AgentError(BUSY) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def _by_pid_locked(root: Path) -> Generator[None]:
    """Hold `by-pid/.lock` so `remove` cannot read an entry, lose a race to a new writer, and
    then delete the entry that writer just made."""
    with _file_locked(root, agent_parts("sessions", BY_PID, BY_PID_LOCK)):
        yield


def write_by_pid(root: Path, pid: int | str, session_id: str) -> Path:
    path = by_pid_path(root, pid)
    with _by_pid_locked(root):
        atomic_write(path, session_id + "\n")
    return path


def read_by_pid(root: Path, pid: int | str) -> str | None:
    try:
        path = by_pid_path(root, pid)
    except ValueError:
        return None
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def get(root: Path, session_id: str) -> dict[str, Any] | None:
    path = _path(root, session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} is not valid JSON: {exc}") from exc


_KNOWN_RECORD_FIELDS = {"session_id", "feature", "set_at", "cwd", "name", "started_at", "dispatch_id", "pid"}


def set_record(
    root: Path,
    session_id: str,
    feature: dict[str, Any] | None = None,
    cwd: str | None = None,
    name: str | None = None,
    clear_feature: bool = False,
    dispatch_id: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    """Upsert the session record. `dispatch_id` marks a worker session pre-mapped by
    `agent dispatch new`. Captures for it carry the id. `pid` is the
    Claude process this session belongs to. The session-start hook passes the same pid it
    writes to `.agent/sessions/by-pid/`. That keeps the two in agreement, so `session list`
    can report LIVE/DEAD.

    Preserves any field this function doesn't itself manage (e.g. thrash-guard's `thrash`
    sub-dict, written by `set_thrash`), instead of dropping it on the next session-start
    mapping call. The old code rebuilt the record from a fixed field list instead."""

    def change(existing: dict[str, Any]) -> dict[str, Any]:
        ts = iso(now())
        record = {k: v for k, v in existing.items() if k not in _KNOWN_RECORD_FIELDS}
        record.update(
            {
                "session_id": session_id,
                "feature": existing.get("feature"),
                "set_at": existing.get("set_at"),
                "cwd": cwd if cwd is not None else existing.get("cwd"),
                "name": name if name is not None else existing.get("name"),
                "started_at": existing.get("started_at") or ts,
                "dispatch_id": dispatch_id if dispatch_id is not None else existing.get("dispatch_id"),
                "pid": pid if pid is not None else existing.get("pid"),
            }
        )
        if feature is not None:
            record["feature"] = feature
            record["set_at"] = ts
        elif clear_feature:
            record["feature"] = None
            record["set_at"] = ts
        if record["set_at"] is None:
            record["set_at"] = ts
        return record

    return update_record(root, session_id, change)


class LockDeadline(AgentError):
    """A session-lock wait ended by the `lock_budget` scope rather than the caller's `timeout`."""


class LockBudget:
    """Seconds a scope may still spend waiting on session locks, and whether a wait ran out."""

    def __init__(self, seconds: float) -> None:
        self.left = seconds
        self.cut = False


_LOCK_BUDGET: contextvars.ContextVar[LockBudget | None] = contextvars.ContextVar(
    "session_lock_budget", default=None
)


@contextlib.contextmanager
def lock_budget(seconds: float) -> Generator[LockBudget]:
    """Bound the total time this thread waits on session locks inside the block. Only waiting
    spends it: a free lock is always taken, so slow work between writes cannot cost a write. A
    nested scope shares the outer budget."""
    outer = _LOCK_BUDGET.get()
    if outer is not None:
        yield outer
        return
    budget = LockBudget(seconds)
    token = _LOCK_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _LOCK_BUDGET.reset(token)


@contextlib.contextmanager
def _locked(root: Path, session_id: str, timeout: float | None = None) -> Generator[None]:
    """Hold `<sid>.lock` for an entire session read-modify-write so concurrent hooks cannot overwrite
    each other's fields. Writes take milliseconds. Callers with their own deadline pass `timeout`;
    inside `lock_budget` the wait is also bounded by what is left of that budget. A spent budget
    still makes one non-blocking attempt; once a wait has been cut, every later write in the scope
    is skipped without trying the lock.
    """
    parts = agent_parts("sessions", f"{session_id}.lock")
    budget = _LOCK_BUDGET.get()
    if budget is None:
        with _file_locked(root, parts, timeout):
            yield
        return
    if budget.cut:
        raise LockDeadline(BUSY)
    explicit = timeout is not None and timeout <= budget.left
    began = time.monotonic()
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(_file_locked(root, parts, timeout if explicit else budget.left))
        except AgentError as exc:
            if explicit or str(exc) != BUSY:
                raise
            budget.cut = True
            raise LockDeadline(BUSY) from None
        finally:
            budget.left = max(0.0, budget.left - (time.monotonic() - began))
        yield


def update_record(
    root: Path,
    session_id: str,
    change: Callable[[dict[str, Any]], dict[str, Any]],
    timeout: float | None = None,
) -> dict[str, Any]:
    """Read, apply `change`, and write under `_locked`; unchanged records cause no write. Before
    creating, changing or ending an owner obligation, record a watched snapshot intent so a
    watchdog publishes it if the writer stalls or dies.
    """
    with _locked(root, session_id, timeout):
        existing = get(root, session_id) or {"session_id": session_id}
        record = change(existing)
        if record is not existing:
            from .vault import live

            intent = None
            if live.owner_obligation(existing) != live.owner_obligation(record):
                intent = live.begin_intent(root, watch=True)
            atomic_write(_path(root, session_id), json.dumps(record, indent=2) + "\n")
            live.fact_persisted(intent)
    return record


def get_thrash(root: Path, session_id: str) -> dict[str, Any]:
    """thrash-guard's counters for this session (`.agent/sessions/<sid>.json`'s `thrash`
    key), or an empty dict for a session with none recorded yet."""
    return dict(as_dict((get(root, session_id) or {}).get("thrash")))


def get_counter(root: Path, session_id: str, key: str) -> int:
    """A small integer counter on the session record (`ticks`). Same storage
    discipline as `get_thrash`: a key `set_record` does not manage, so it survives every
    mapping call."""
    try:
        return int((get(root, session_id) or {}).get(key) or 0)
    except ValueError, TypeError:
        return 0


def set_counter(root: Path, session_id: str, key: str, value: int, timeout: float | None = None) -> None:
    """Store a small integer counter independently of feature mapping, as with `get_thrash`."""
    update_record(root, session_id, lambda existing: {**existing, key: int(value)}, timeout)


def get_session_list(root: Path, session_id: str, key: str) -> list[str]:
    """Store a per-session list of strings independently of feature mapping; absent records start
    empty.
    """
    return [str(v) for v in as_list((get(root, session_id) or {}).get(key))]


def add_to_session_list(root: Path, session_id: str, key: str, value: str) -> list[str]:
    """Append to such a list, deduped, without touching the mapping or `set_at`."""

    def change(existing: dict[str, Any]) -> dict[str, Any]:
        items = [str(v) for v in as_list(existing.get(key))]
        if value not in items:
            items.append(value)
        return {**existing, key: items}

    return list(update_record(root, session_id, change)[key])


def set_field(root: Path, session_id: str, key: str, value: Any, timeout: float | None = None) -> None:
    """Write an unmanaged field without changing feature mapping or `set_at`."""
    update_record(root, session_id, lambda existing: {**existing, key: value}, timeout)


def record_cost(
    root: Path,
    session_id: str,
    total_usd: float,
    day: str,
    *,
    source: str = "provider-result",
    priced_at: str | None = None,
) -> dict[str, Any] | None:
    """Record a manager's running total and provenance, estimated from its session files on Stop.

    Two fields. `cost_usd` is that running total. `cost_by_day` is `{YYYY-MM-DD: usd}`, built
    from the increase since the last call, so a session working past midnight splits across the
    two days the work happened on rather than landing whole on the day it started. A decrease
    (a resumed session whose total restarts) adds nothing. The first call for a session books
    its whole running total to that day, which is only wrong for a session that was already
    running before this was ever called: the day it is first seen takes what earlier days
    spent. That is the transition, not the steady state.

    A forced unknown clears that session's earlier daily amounts. A lower estimate replacing
    a higher provider-result total keeps the old days. A forked session's first Stop books its
    copied history again. These are accepted effects of recording cumulative session totals.

    Returns None and writes nothing when the total and provenance have not moved by a cent.
    """
    previous = 0.0
    record = get(root, session_id)
    if record:
        try:
            previous = float(record.get("cost_usd") or 0.0)
        except ValueError, TypeError:
            previous = 0.0
    if (
        round(total_usd - previous, 2) <= 0
        and record
        and record.get("cost_source") == source
        and record.get("cost_priced_at") == priced_at
    ):
        return None

    def change(existing: dict[str, Any]) -> dict[str, Any]:
        # re-read under the lock: `previous` above is only a cheap check for whether to write
        try:
            held = float(existing.get("cost_usd") or 0.0)
        except ValueError, TypeError:
            held = 0.0
        by_day = dict(as_dict(existing.get("cost_by_day")))
        delta = max(total_usd - held, 0.0)
        by_day[day] = round(as_float(by_day.get(day)) + delta, 4)
        return {
            **existing,
            "cost_usd": round(total_usd, 4),
            "cost_source": source,
            "cost_priced_at": priced_at,
            "cost_by_day": by_day,
        }

    return update_record(root, session_id, change)


def mark_cost_unknown(root: Path, session_id: str, day: str, *, force: bool = False) -> None:
    """Mark an unpriced session/day unknown without replacing a known figure by default."""

    def change(existing: dict[str, Any]) -> dict[str, Any]:
        by_day = dict(as_dict(existing.get("cost_by_day")))
        if day in by_day and not force:
            return existing
        if force:
            # The cumulative estimate is now unpriceable (for example after a model switch).
            # No earlier day can claim a complete amount from this session anymore.
            by_day = dict.fromkeys(by_day)
        by_day[day] = None
        return {
            **existing,
            "cost_usd": None if force else existing.get("cost_usd"),
            "cost_source": "unknown",
            "cost_priced_at": None if force else existing.get("cost_priced_at"),
            "cost_by_day": by_day,
        }

    update_record(root, session_id, change)


def cost_summary_for_day(root: Path, day: str) -> tuple[float, bool, bool]:
    """Known manager spend, whether every entry is priced, and whether any entry exists."""
    total = 0.0
    complete = True
    found = False
    for record in all_records(root):
        by_day = as_dict_or_none(record.get("cost_by_day"))
        if by_day is None or day not in by_day:
            continue
        found = True
        amount = as_float_or_none(by_day[day])
        if amount is None:
            complete = False
            continue
        total += amount
    return round(total, 4), complete, found


def set_thrash(root: Path, session_id: str, thrash: dict[str, Any]) -> None:
    """Persist thrash-guard's counters without touching feature mapping or `set_at`. This
    fires on nearly every tool call, far more often than a real `feature_use`. It must not
    look like one, or bump `started_at`/`set_at` on a session that already has them."""
    update_record(root, session_id, lambda existing: {**existing, "thrash": thrash})


def prune(root: Path, days: int = 14, keep: Callable[[dict[str, Any]], bool] | None = None) -> list[str]:
    """Remove records older than `days`, except any `keep` says to keep (the op keeps live ones)."""
    base = sessions_dir(root)
    if not base.exists():
        return []
    cutoff = now() - timedelta(days=days)
    removed: list[str] = []
    for path in base.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            when = record.get("set_at") or record.get("started_at")
        except json.JSONDecodeError, OSError:
            record, when = {}, None
        if when is None:
            continue
        from datetime import datetime

        try:
            ts = datetime.fromisoformat(when)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        if ts < cutoff and remove(root, path.stem, keep=keep) is not None:
            removed.append(path.stem)
    return removed


def remove(
    root: Path, session_id: str, keep: Callable[[dict[str, Any]], bool] | None = None
) -> list[str] | None:
    """Delete the session and by-pid entries still naming it; return removed pids.

    Recheck `keep` under the session lock. Keep lock files stable: unlinking a held lock
    lets another writer lock a new inode. Hold `<sid>.lock` then `by-pid/.lock` through
    cleanup; `write_by_pid` takes only the latter, so reassignment cannot be deleted.
    Refuse removal where neither POSIX nor Windows file locks are available.
    """
    if not _file_lock_supported():
        raise AgentError("session removal needs file locking this platform lacks; nothing was removed.")
    with _locked(root, session_id):
        record = get(root, session_id)
        if record is not None and keep is not None and keep(record):
            return None
        _path(root, session_id).unlink(missing_ok=True)
        cleared: list[str] = []
        by_pid = sessions_dir(root) / BY_PID
        if not by_pid.is_dir():
            return cleared
        with _by_pid_locked(root):
            for entry in by_pid.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.read_text(encoding="utf-8").strip() == session_id:
                        entry.unlink()
                        cleared.append(entry.name)
                except OSError:
                    continue
        return cleared


def all_records(root: Path) -> list[dict[str, Any]]:
    base = sessions_dir(root)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in json_files(base):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            if reads_strict():  # a partial view would be trusted (core.strict_reads)
                raise
            continue
    return out
