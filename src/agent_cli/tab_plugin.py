"""The agent-terminal-focus plugin as the tab painter.

The plugin polls `.agent/live.json` every 2 s and sets each terminal tab's title from the
`tabs` entries `vault/live.py` writes: the label is computed here, in Python, by the same
`activity.desired_label` the hooks use, so the plugin only matches a tab and copies a string
(see ADR-0015). It leaves two files behind in `.agent/tabs/`:

- `plugin.json`, its heartbeat: when it last polled, whether that poll read `live.json`
  (`read_ok`) and when one last did (`last_ok_at`), and what it last painted per tab key. While
  it is fresh and healthy (`active`), `tab_drain` skips the `obsidian eval` relabel push, which
  stays only as a fallback for one release (slice 2 retires it). The push resumes by itself
  once it is not: Obsidian closed, the plugin disabled or unable to read the snapshot, or an
  old plugin build.
- `plugin-metrics.jsonl`, one row per poll that tried to change a tab or failed to read the
  snapshot, so doctor can report the paint failure rate (slice-1 exit criterion: under 2%).

While a snapshot write has failed the plugin yields, and its heartbeat says so with `yielded`
and its `protocol`. Residual: a plugin build from before that protocol cannot be told to stop,
so it may repaint the stale snapshot over the fallback until `agent setup` installs the
current build; `status check` and doctor say so while one runs (`outdated`).
"""

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
"""The plugin rewrites its heartbeat at least every 10 s while Obsidian runs, so three missed
writes mean it has stopped and the eval push takes over again."""


def heartbeat(root: Path) -> dict[str, Any]:
    try:
        return as_dict(json.loads((root / HEARTBEAT).read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}


def _stamp(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _healthy_since(beat: dict[str, Any]) -> float | None:
    """The older of the heartbeat's `at` and `last_ok_at`, when its last poll read the snapshot
    (`read_ok`); else None. A plugin that polls but cannot read `live.json` paints nothing, so
    its heartbeat must not hold the fallback back; a build that
    writes no `read_ok` does not count either. Nor does a plugin that has `yielded`: it saw the
    snapshot-write failure record (`live.write_failed`) and stopped painting a snapshot that no
    longer holds the facts, so the fallback paints."""
    at, ok_at = _stamp(beat.get("at")), _stamp(beat.get("last_ok_at"))
    if at is None or ok_at is None or beat.get("read_ok") is not True or beat.get("yielded") is True:
        return None
    return min(at, ok_at)


YIELD_PROTOCOL = 2
"""The heartbeat's `protocol` from the plugin build that yields while a snapshot write has
failed. A build without it keeps painting the stale snapshot then."""
OUTDATED = "plugin build predates the yield protocol, run agent setup"


def predates_yield(beat: dict[str, Any]) -> bool:
    protocol = beat.get("protocol")
    return not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < YIELD_PROTOCOL


def active(root: Path, now: float | None = None) -> bool:
    """Whether the plugin is painting tabs now: its heartbeat and its last good read of
    `live.json` are both younger than `HEARTBEAT_STALE_S`, and its last poll read the snapshot.
    A heartbeat from the future (a clock step) does not count.

    While a snapshot write has failed, a build older than `YIELD_PROTOCOL` counts out too
    : it cannot yield, so the fallback must paint without
    waiting for it. It cannot be stopped from repainting the stale snapshot either, which
    `outdated` surfaces to `status check` and doctor until it is updated."""
    beat = heartbeat(root)
    since = _healthy_since(beat)
    if since is None:
        return False
    age = (now if now is not None else time.time()) - since
    if not 0 <= age < HEARTBEAT_STALE_S:
        return False
    if predates_yield(beat):
        from .vault import live

        return not live.yield_due(root)  # a write in flight is painted through
    return True


def read_problem(root: Path, now: float | None = None) -> str | None:
    """What a running plugin's last poll could not read (`live.json`, or the failure folder, which then counts as a failure standing), else None."""
    beat = heartbeat(root)
    at = _stamp(beat.get("at"))
    if at is None or not 0 <= (now if now is not None else time.time()) - at < HEARTBEAT_STALE_S:
        return None
    error = beat.get("read_error")
    return error if isinstance(error, str) and error else None


def outdated(root: Path, now: float | None = None) -> bool:
    """A plugin is running (its heartbeat is fresh) whose build predates `YIELD_PROTOCOL`."""
    beat = heartbeat(root)
    at = _stamp(beat.get("at"))
    if at is None or not 0 <= (now if now is not None else time.time()) - at < HEARTBEAT_STALE_S:
        return False
    return predates_yield(beat)


def expires_at(root: Path) -> float:
    """When the plugin stops counting as active (epoch seconds), plus a second; now if it
    does not count now."""
    since = _healthy_since(heartbeat(root))
    if since is None:
        return time.time()
    return since + HEARTBEAT_STALE_S + 1


def paint_rate(root: Path, window_s: float = 86400.0, now: float | None = None) -> dict[str, int]:
    """The plugin's tab paints in the last `window_s`: how many it attempted, how many failed,
    and how many polls could not read `live.json`."""
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
