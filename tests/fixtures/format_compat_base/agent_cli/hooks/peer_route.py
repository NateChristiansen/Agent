# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
from .. import sessions
from ..records.narrow import as_dict_or_none
from .paths import decision, deny


def _manager(root: Path, target: str) -> dict[str, Any] | None:
    wanted = target.strip()
    if not wanted:
        return None
    for record in sessions.all_records(root):
        if record.get("dispatch_id"):
            continue
        sid = str(record.get("session_id") or "")
        name = str(record.get("name") or "")
        if wanted in (name, sid) or (len(wanted) >= 6 and sid.startswith(wanted)):
            return record
    return None


def peer_route(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("tool_name") != "SendMessage":
        return ""
    data = as_dict_or_none(payload.get("tool_input") or {})
    if data is None:
        return ""
    target = str(data.get("to") or "")
    try:
        record = _manager(root, target)
    except Exception:
        return ""
    if record is None:
        return ""
    name = str(record.get("name") or record.get("session_id"))
    return deny(
        f"{name} is a harness manager session; SendMessage does not reach managers across clients and leaves no inbox record. Use the `peer` MCP tool (action send, to {name}) instead; action `list` shows who is reachable."
    )


PLAN_MODE_TOOLS = frozenset(
    {
        "mcp__agent__peer",
        "mcp__agent__feature_use",
        "mcp__agent__feature_current",
        "mcp__agent__session_get",
        "mcp__agent__dispatch_read",
    }
)
PLAN_MODE_DISPATCH = ("mcp__agent__dispatch", "plan-review")


def plan_mode_allow(root: Path, payload: dict[str, Any]) -> str:
    tool = payload.get("tool_name")
    if tool == PLAN_MODE_DISPATCH[0]:
        data = as_dict_or_none(payload.get("tool_input")) or {}
        if data.get("action") != PLAN_MODE_DISPATCH[1]:
            return ""
        return decision("allow", "a plan's read-only review may launch in plan mode")
    if tool not in PLAN_MODE_TOOLS:
        return ""
    return decision("allow", "session mapping and peer messages are allowed in every mode")
