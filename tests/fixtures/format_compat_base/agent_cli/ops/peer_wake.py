# ruff: noqa
from __future__ import annotations
import os
import uuid
from pathlib import Path
from typing import Any, Literal
from .. import peers as peers_mod
from .. import sessions as sessions_mod
from ..core import now
from ..records.narrow import as_dict

WAKE_TIMEOUT_S = 3.0
WAKE_ATTEMPT_TTL_S = WAKE_TIMEOUT_S * 2
WAKE_ACCEPTED_TTL_S = 30 * 60
WAKE_TEXT = "Peer message in this turn's context. Reply with the `peer` tool, action `send`, only if it asks for something. If absent, call it with action `inbox`."
DISPATCH_WAKE_TEXT = "Dispatch supervisor notice in this turn's context. Read the authoritative record with the dispatch tool, action show. No peer reply is required. If absent, the dispatch tool's action list names the waiting record; a normal owner prompt retains the pending-record fallback."
WakePurpose = Literal["peer", "dispatch"]
WAKE_TEXTS: dict[str, str] = {"peer": WAKE_TEXT, "dispatch": DISPATCH_WAKE_TEXT}


def wake_text(purpose: WakePurpose) -> str:
    if purpose not in WAKE_TEXTS:
        raise ValueError(f"unknown wake purpose {purpose!r}")
    return WAKE_TEXTS[purpose]


def dispatch_ring_suppressed() -> bool:
    from ..dispatch.notify import notifications_enabled

    return not notifications_enabled() or bool(os.environ.get("AGENT_NOW"))


def wake_current(state: dict[str, Any]) -> bool:
    status = state.get("state")
    ttl = WAKE_ATTEMPT_TTL_S if status == "attempting" else WAKE_ACCEPTED_TTL_S if status == "accepted" else 0
    recorded = state.get("at")
    if not isinstance(recorded, (int, float, str)):
        return False
    try:
        age = now().timestamp() - float(recorded)
    except TypeError, ValueError:
        return False
    return ttl > 0 and 0 <= age < ttl


def reserve(root: Path, target: str, *, timeout: float | None = None) -> str | None:
    watermark = peers_mod.arrivals(root, target)
    prior = as_dict((sessions_mod.get(root, target) or {}).get(peers_mod.WAKE_FIELD))
    if wake_current(prior) and (not peers_mod.marked_stale(root, target, prior)):
        if prior.get("watermark") != watermark:
            extended = {**prior, "watermark": watermark}
            sessions_mod.set_field(root, target, peers_mod.WAKE_FIELD, extended, timeout)
        return None
    wake_id = uuid.uuid4().hex
    reservation = {"id": wake_id, "state": "attempting", "at": now().timestamp(), "watermark": watermark}
    sessions_mod.set_field(root, target, peers_mod.WAKE_FIELD, reservation, timeout)
    return wake_id


def settle(root: Path, target: str, wake_id: str, accepted: bool, *, timeout: float | None = None) -> None:
    deadline = peers_mod.deadline_after(timeout)
    with peers_mod.inbox_guard(root, target, timeout=timeout):
        current = as_dict((sessions_mod.get(root, target) or {}).get(peers_mod.WAKE_FIELD))
        if current.get("id") == wake_id:
            outcome = {**current, "state": "accepted" if accepted else "failed", "at": now().timestamp()}
            sessions_mod.set_field(root, target, peers_mod.WAKE_FIELD, outcome, peers_mod.time_left(deadline))
