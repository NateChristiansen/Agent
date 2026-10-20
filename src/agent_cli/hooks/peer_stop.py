"""`peer-stop`: a Claude manager does not end a turn with peer messages unread.

A Claude wake is one socket frame. A frame that lands while the session is busy is handled in
that turn, and a message queued after the last prompt hook of a turn waits for the next ring,
which a stale reservation could suppress for 30 minutes (seven messages from the
Codex manager sat until the owner typed). Turn end is the last moment the session is sure to
read anything, so this Stop hook takes what is waiting and blocks the stop once with it: the
session continues and answers. `stop_hook_active` lets the continued turn end, so one message
cannot loop the session; anything that arrives during it rings as usual, because the take
acknowledged the reservation. Wired on Claude only: Codex wakes through a durable queue.

Dispatch supervisor notices are taken in the same read. Each is tombstoned
before the hook answers, so it blocks at most one Stop, a retried hook included; a later notice
may block once. A busy guard or a failed save blocks nothing and lets the turn end.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import peers
from . import notice_inbox

#: The count line each injected block opens with (`notice_inbox.block`, `peers.block`).
_COUNTED = re.compile(
    rf"^## ({re.escape(notice_inbox.HEADING)}|Messages from other sessions) \((\d+)\)$", re.MULTILINE
)


def logged_reason(reason: str) -> str:
    """What `decision_log` keeps of a peer-stop block reason: a fixed line and the number of
    notices and messages delivered, never their text, which goes only to the model (the denial log copied peer message text). Each block's first count line is its own; a line
    of the same shape inside a message is not counted."""
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
    except Exception:  # noqa: BLE001 — an unreadable inbox must not trap a turn
        return ""
    blocks = [b for b in (notice_inbox.block(consumed), peers.block(rows)) if b]
    if rows:
        from .notes_sync import evidence_lines

        blocks += evidence_lines(root, "\n".join(str(r.get("text") or "") for r in rows), session_id)
    if not blocks:
        return ""
    return json.dumps({"decision": "block", "reason": "\n\n".join(blocks)})
