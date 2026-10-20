# ruff: noqa
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any
from .. import peers
from . import notice_inbox

_COUNTED = re.compile(
    f"^## ({re.escape(notice_inbox.HEADING)}|Messages from other sessions) \\((\\d+)\\)$", re.MULTILINE
)


def logged_reason(reason: str) -> str:
    counts: dict[str, int] = {}
    for kind, n in _COUNTED.findall(reason):
        counts.setdefault(kind, int(n))
    return f"peer-stop: {sum(counts.values())} notices delivered"


def peer_stop(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("stop_hook_active") or payload.get("agent_id") or payload.get("agent_type"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    try:
        rows, consumed = notice_inbox.take(root, session_id, acknowledge_wake=True, via="stop")
    except Exception:
        return ""
    blocks = [b for b in (notice_inbox.block(consumed), peers.block(rows)) if b]
    if rows:
        from .notes_sync import evidence_lines

        blocks += evidence_lines(root, "\n".join((str(r.get("text") or "") for r in rows)), session_id)
    if not blocks:
        return ""
    return json.dumps({"decision": "block", "reason": "\n\n".join(blocks)})
