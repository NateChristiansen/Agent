# ruff: noqa
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from ..records.narrow import as_dict, as_dict_or_none, as_int, as_str
from .scope import Scope
from .tally import Tally, day_start, local_date, records, touched_since

FOLDERS = ("sessions", "archived_sessions")
DRAFTER_PATH = "journal_drafter"
_STARTED_RE = re.compile("^rollout-(\\d{4}-\\d{2}-\\d{2})T")


@dataclass
class Thread:
    path: Path
    cwd: str
    parent: str
    agent_path: str = ""


def codex_home(home: Path) -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else home / ".codex"


def _started(path: Path) -> date | None:
    try:
        return date.fromisoformat(f"{path.parents[2].name}-{path.parents[1].name}-{path.parent.name}")
    except ValueError, IndexError:
        pass
    match = _STARTED_RE.match(path.name)
    try:
        return date.fromisoformat(match.group(1)) if match else None
    except ValueError:
        return None


def rollouts(sessions: Path, day: date) -> list[Path]:
    since = day_start(day)
    out: list[Path] = []
    for path in sorted(sessions.glob("**/rollout-*.jsonl")):
        started = _started(path)
        if started is not None and started <= day and touched_since(path, since):
            out.append(path)
    return out


def _thread(path: Path, tally: Tally) -> tuple[str, Thread] | None:
    for record in records(path, tally):
        if record.get("type") == "session_meta":
            payload = as_dict(record.get("payload"))
            parent = as_str(payload.get("parent_thread_id"))
            spawn = as_dict(as_dict(as_dict(payload.get("source")).get("subagent")).get("thread_spawn"))
            if not parent:
                parent = as_str(spawn.get("parent_thread_id"))
            thread = Thread(path, as_str(payload.get("cwd")), parent, as_str(spawn.get("agent_path")))
            return (as_str(payload.get("id")), thread)
    return None


def thread_class(
    thread_id: str, thread: Thread, threads: dict[str, Thread], kind: str, dispatches: dict[str, str]
) -> str:
    current = thread_id
    parent = thread.parent
    visited: set[str] = set()
    drafter = thread.agent_path.rstrip("/").endswith(DRAFTER_PATH)
    while True:
        if current in dispatches:
            return dispatches[current]
        if not parent or parent in visited:
            break
        visited.add(current)
        current = parent
        above = threads.get(parent)
        drafter = drafter or bool(above and above.agent_path.rstrip("/").endswith(DRAFTER_PATH))
        parent = above.parent if above else ""
    if kind == "eval":
        return "eval"
    if drafter:
        return "drafter"
    if kind == "vault":
        return "subagent" if thread.parent else "manager"
    return "other"


def _add(tally: Tally, cls: str, model: str, usage: dict[str, object]) -> None:
    input_tokens = as_int(usage.get("input_tokens"))
    cache_read = as_int(usage.get("cached_input_tokens"))
    cache_write = as_int(usage.get("cache_write_input_tokens"))
    tally.add(
        cls,
        model,
        max(input_tokens - cache_read - cache_write, 0),
        as_int(usage.get("output_tokens")),
        cache_write,
        cache_read,
    )


def _count_file(path: Path, day: date, cls: str, tally: Tally, seen: set[str]) -> bool:
    rows: list[dict[str, Any]] = list(records(path, tally))
    new_schema = any((row.get("type") == "token_usage_record" for row in rows))
    model_by_turn: dict[str, str] = {}
    model = "unknown"
    previous_total: object = None
    counted = False
    for row in rows:
        kind = row.get("type")
        payload = as_dict(row.get("payload"))
        if kind == "turn_context":
            model = as_str(payload.get("model")) or "unknown"
            if turn := as_str(payload.get("turn_id")):
                model_by_turn[turn] = model
            continue
        on_day = local_date(row.get("timestamp")) == day
        if new_schema and kind == "token_usage_record":
            response_id = as_str(payload.get("response_id"))
            usage = as_dict_or_none(payload.get("usage"))
            if usage is None or (response_id and response_id in seen):
                continue
            if response_id:
                seen.add(response_id)
            if on_day:
                _add(tally, cls, model_by_turn.get(as_str(payload.get("turn_id")), model), usage)
                counted = True
        elif not new_schema and kind == "event_msg" and (payload.get("type") == "token_count"):
            info = as_dict(payload.get("info"))
            total = info.get("total_token_usage")
            usage = as_dict_or_none(info.get("last_token_usage"))
            if usage is None or (total is not None and total == previous_total):
                continue
            previous_total = total
            if on_day:
                _add(tally, cls, model, usage)
                counted = True
    return counted


def codex_usage(day: date, scope: Scope, dispatches: dict[str, str], home: Path) -> Tally:
    folders = [codex_home(home) / name for name in FOLDERS]
    present = [folder for folder in folders if folder.exists()]
    tally = Tally(absent="" if present else str(folders[0]))
    paths: list[Path] = []
    names: set[str] = set()
    for folder in present:
        try:
            next(folder.iterdir(), None)
            found = rollouts(folder, day)
        except OSError as exc:
            return Tally(status="unavailable", reason=f"cannot list Codex rollouts in {folder}: {exc}")
        paths += [path for path in found if path.name not in names]
        names.update((path.name for path in found))
    found = [pair for path in paths if (pair := _thread(path, tally))]
    threads = {thread_id: thread for thread_id, thread in reversed(found) if thread_id}
    seen: set[str] = set()
    for thread_id, thread in found:
        kind = scope.kind(thread.cwd)
        if kind is None:
            continue
        cls = thread_class(thread_id, thread, threads, kind, dispatches)
        tally.files += _count_file(thread.path, day, cls, tally, seen)
    return tally.finish()
