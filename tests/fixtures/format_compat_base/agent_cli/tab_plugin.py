# ruff: noqa
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any
from .core import agent_path
from .records.narrow import as_dict, as_int

HEARTBEAT = agent_path(Path(), "tabs", "plugin.json")
METRICS = agent_path(Path(), "tabs", "plugin-metrics.jsonl")
HEARTBEAT_STALE_S = 30.0
"The plugin rewrites its heartbeat at least every 10 s while Obsidian runs, so three missed\nwrites mean it has stopped and the eval push takes over again."


def heartbeat(root: Path) -> dict[str, Any]:
    try:
        return as_dict(json.loads((root / HEARTBEAT).read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}


def _stamp(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and (not isinstance(value, bool)) else None


def _healthy_since(beat: dict[str, Any]) -> float | None:
    at, ok_at = (_stamp(beat.get("at")), _stamp(beat.get("last_ok_at")))
    if at is None or ok_at is None or beat.get("read_ok") is not True or (beat.get("yielded") is True):
        return None
    return min(at, ok_at)


YIELD_PROTOCOL = 2
"The heartbeat's `protocol` from the plugin build that yields while a snapshot write has\nfailed. A build without it keeps painting the stale snapshot then."
OUTDATED = "plugin build predates the yield protocol, run agent setup"


def predates_yield(beat: dict[str, Any]) -> bool:
    protocol = beat.get("protocol")
    return not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < YIELD_PROTOCOL


def active(root: Path, now: float | None = None) -> bool:
    beat = heartbeat(root)
    since = _healthy_since(beat)
    if since is None:
        return False
    age = (now if now is not None else time.time()) - since
    if not 0 <= age < HEARTBEAT_STALE_S:
        return False
    if predates_yield(beat):
        from .vault import live

        return not live.yield_due(root)
    return True


def read_problem(root: Path, now: float | None = None) -> str | None:
    beat = heartbeat(root)
    at = _stamp(beat.get("at"))
    if at is None or not 0 <= (now if now is not None else time.time()) - at < HEARTBEAT_STALE_S:
        return None
    error = beat.get("read_error")
    return error if isinstance(error, str) and error else None


def outdated(root: Path, now: float | None = None) -> bool:
    beat = heartbeat(root)
    at = _stamp(beat.get("at"))
    if at is None or not 0 <= (now if now is not None else time.time()) - at < HEARTBEAT_STALE_S:
        return False
    return predates_yield(beat)


def expires_at(root: Path) -> float:
    since = _healthy_since(heartbeat(root))
    if since is None:
        return time.time()
    return since + HEARTBEAT_STALE_S + 1


def paint_rate(root: Path, window_s: float = 86400.0, now: float | None = None) -> dict[str, int]:
    cutoff = (now if now is not None else time.time()) - window_s
    attempted = failed = read_errors = 0
    try:
        lines = (root / METRICS).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = as_dict(json.loads(line))
        except ValueError:
            continue
        at = row.get("at")
        if not isinstance(at, (int, float)) or at < cutoff:
            continue
        attempted += as_int(row.get("attempted"))
        failed += as_int(row.get("failed"))
        read_errors += 1 if row.get("read_error") else 0
    return {"attempted": attempted, "failed": failed, "read_errors": read_errors}
