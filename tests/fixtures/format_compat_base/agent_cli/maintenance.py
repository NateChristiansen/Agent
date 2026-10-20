# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
from . import config_file
from .core import AgentError
from .records.narrow import as_dict_or_none, as_list_or_none

OPS = ("launch", "escalate", "resume", "auto_resume")


def settings(root: Path) -> dict[str, Any]:
    raw = config_file.machine_section(root, "maintenance")
    if raw is None:
        return {}
    where = f"{config_file.machine_root(root) / config_file.LOCAL}: maintenance"
    block = as_dict_or_none(raw)
    if block is None or set(block) - {"window", "parked", "reason"}:
        raise AgentError(f"{where} takes only `window`, `parked` and `reason`")
    window, reason, given = (block.get("window"), block.get("reason"), block.get("parked"))
    parked = as_list_or_none(given) if given else []
    if window is not None and (not (isinstance(window, str) and window.strip())):
        raise AgentError(f"{where}.window must be a non-empty reason")
    if reason is not None and (not isinstance(reason, str)):
        raise AgentError(f"{where}.reason must be text")
    if parked is None or not all((isinstance(p, (str, int)) for p in parked)):
        raise AgentError(f"{where}.parked must list feature numbers")
    try:
        numbers = [f"{int(p):03d}" for p in parked if isinstance(p, (str, int))]
    except ValueError as exc:
        raise AgentError(f"{where}.parked must list feature numbers") from exc
    return {"window": window, "parked": numbers, "reason": reason}


def parked(root: Path) -> list[str]:
    try:
        return list(settings(root).get("parked") or [])
    except AgentError:
        return []


def check(root: Path, feature: int | str | None, op: str) -> None:
    if op not in OPS:
        raise ValueError(f"unknown dispatch start {op!r}")
    cfg = settings(root)
    if not cfg:
        return
    if cfg["window"]:
        raise AgentError(
            f"dispatch {op} refused: maintenance window ({cfg['window']}). The owner clears `maintenance.window` in agent.local.yaml when it is over."
        )
    if feature is None:
        return
    number = f"{int(feature):03d}"
    if number in cfg["parked"]:
        why = f" ({cfg['reason']})" if cfg["reason"] else ""
        raise AgentError(
            f"feature {number} is parked{why}; dispatch {op} refused. Restart it as its plan's restart note says, from a fresh worktree off repaired main; the owner then removes {number} from `maintenance.parked` in agent.local.yaml."
        )
