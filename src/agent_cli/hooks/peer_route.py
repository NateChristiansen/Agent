"""Route managers through the shared peer inbox rather than client-local SendMessage. The inbox
supports both clients and paired devices, and survives recipient turn boundaries.
"""

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
            continue  # a worker: SendMessage is its channel
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
    except Exception:  # noqa: BLE001 — fail open
        return ""
    if record is None:
        return ""
    name = str(record.get("name") or record.get("session_id"))
    return deny(
        f"{name} is a harness manager session; SendMessage does not reach managers across clients "
        f"and leaves no inbox record. Use the `peer` MCP tool (action send, to {name}) instead; "
        "action `list` shows who is reachable."
    )


PLAN_MODE_TOOLS = frozenset(
    {
        "mcp__agent__peer",
        "mcp__agent__feature_use",
        "mcp__agent__feature_show",
        "mcp__agent__session_get",
        "mcp__agent__dispatch_read",
    }
)
#: The one `dispatch` action plan mode may run without asking: the plan's own review.
PLAN_MODE_DISPATCH = ("mcp__agent__dispatch", "plan-review")


def plan_mode_allow(root: Path, payload: dict[str, Any]) -> str:
    """Claude PreToolUse on the session bookkeeping tools: allow them, including in plan mode.

    Plan mode asks before every tool that writes, and a manager told "work on <feature>" could not
    map itself while in plan mode. `peer` writes inboxes and `feature_use` writes the session
    mapping, never features or code; `feature_show`, `session_get` and `dispatch_read`
    only read. The owner
    approved exempting them. `dispatch` is allowed only for action `plan-review`
    : a plan's review must launch before ExitPlanMode, so from inside plan mode, and it
    is read-only (`writes=[]`). Every other dispatch action, `feature_new` and every other agent
    tool still ask in plan mode. Claude only: Codex has no plan-mode permission gate."""
    tool = payload.get("tool_name")
    if tool == PLAN_MODE_DISPATCH[0]:
        data = as_dict_or_none(payload.get("tool_input")) or {}
        if data.get("action") != PLAN_MODE_DISPATCH[1]:
            return ""
        return decision("allow", "a plan's read-only review may launch in plan mode")
    if tool not in PLAN_MODE_TOOLS:
        return ""
    return decision("allow", "session mapping and peer messages are allowed in every mode")
