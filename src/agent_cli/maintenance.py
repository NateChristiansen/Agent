"""Maintenance and parked features: local refusals in front of every dispatch start.

`blocked` is still an open status, and launch permits open features, so writing "parked" into a
plan never stopped a manager from resuming stale work. This gate does. It lives in this machine's
`agent.local.yaml`, outside every worker grant and outside MCP, so only the owner clears it:

    maintenance:
      window: "audit repair in progress"   # refuse every dispatch start
      parked: ["023", "026", "029"]        # refuse build and resume for these features
      reason: "WIP archived; restart from repaired main per the plan's restart note"

A refusal raises before any effect: no process, record, lease, claim or task changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config_file
from .core import AgentError
from .records.narrow import as_dict_or_none, as_list_or_none

OPS = ("launch", "escalate", "resume", "auto_resume")


def settings(root: Path) -> dict[str, Any]:
    """The validated `maintenance:` block, or `{}`. A malformed block raises: a gate that
    silently stopped gating would be worse than a refusal."""
    raw = config_file.machine_section(root, "maintenance")
    if raw is None:
        return {}
    where = f"{config_file.machine_root(root) / config_file.LOCAL}: maintenance"
    block = as_dict_or_none(raw)
    if block is None or set(block) - {"window", "parked", "reason"}:
        raise AgentError(f"{where} takes only `window`, `parked` and `reason`")
    window, reason, given = block.get("window"), block.get("reason"), block.get("parked")
    parked = as_list_or_none(given) if given else []
    if window is not None and not (isinstance(window, str) and window.strip()):
        raise AgentError(f"{where}.window must be a non-empty reason")
    if reason is not None and not isinstance(reason, str):
        raise AgentError(f"{where}.reason must be text")
    if parked is None or not all(isinstance(p, (str, int)) for p in parked):
        raise AgentError(f"{where}.parked must list feature numbers")
    try:
        numbers = [f"{int(p):03d}" for p in parked if isinstance(p, (str, int))]
    except ValueError as exc:
        raise AgentError(f"{where}.parked must list feature numbers") from exc
    return {"window": window, "parked": numbers, "reason": reason}


def parked(root: Path) -> list[str]:
    """The parked feature numbers; none when the block is absent or malformed (for views)."""
    try:
        return list(settings(root).get("parked") or [])
    except AgentError:
        return []


def check(root: Path, feature: int | str | None, op: str) -> None:
    """Refuse `op` for `feature` while a window is open or the feature is parked."""
    if op not in OPS:
        raise ValueError(f"unknown dispatch start {op!r}")
    cfg = settings(root)
    if not cfg:
        return
    if cfg["window"]:
        raise AgentError(
            f"dispatch {op} refused: maintenance window ({cfg['window']}). The owner clears "
            "`maintenance.window` in agent.local.yaml when it is over."
        )
    if feature is None:
        return
    number = f"{int(feature):03d}"
    if number in cfg["parked"]:
        why = f" ({cfg['reason']})" if cfg["reason"] else ""
        raise AgentError(
            f"feature {number} is parked{why}; dispatch {op} refused. Restart it as its plan's "
            "restart note says, from a fresh worktree off repaired main; the owner then removes "
            f"{number} from `maintenance.parked` in agent.local.yaml."
        )
