"""Share wake reservations across local messages, remote enqueues and dispatch notices so concurrent
senders do not ring twice. Hold peers.inbox_guard around reservation. Fixed purpose-specific wake
text lives here; durable queues remain separate.
"""

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

# Name the typed inbox tool in wake guidance for both clients. Models use MCP rather than shell CLI
# calls.
WAKE_TEXT = (
    "Peer message in this turn's context. Reply with the `peer` tool, action `send`, only if it "
    "asks for something. If absent, call it with action `inbox`."
)
DISPATCH_WAKE_TEXT = (
    "Dispatch supervisor notice in this turn's context. Read the authoritative record with the "
    "dispatch tool, action show. No peer reply is required. If absent, the dispatch tool's "
    "action list names the waiting record; a normal owner prompt retains the pending-record fallback."
)

WakePurpose = Literal["peer", "dispatch"]
WAKE_TEXTS: dict[str, str] = {"peer": WAKE_TEXT, "dispatch": DISPATCH_WAKE_TEXT}


def wake_text(purpose: WakePurpose) -> str:
    """The fixed text a ring carries for `purpose`. Anything else is refused, never sent."""
    if purpose not in WAKE_TEXTS:
        raise ValueError(f"unknown wake purpose {purpose!r}")
    return WAKE_TEXTS[purpose]


def dispatch_ring_suppressed() -> bool:
    """Dispatch rings are off with notifications off or under the test clock. Peer rings are not."""
    from ..dispatch.notify import notifications_enabled

    return not notifications_enabled() or bool(os.environ.get("AGENT_NOW"))


def wake_current(state: dict[str, Any]) -> bool:
    """Whether another send should reuse this wake reservation.

    An unfinished request expires after two command timeouts, so a killed sender cannot
    strand the inbox. Socket timeout applies per operation, so a slow connect followed by
    a slow send could exceed this TTL and cause one extra wake. An accepted request is not
    a delivery receipt; retry after 30 minutes of unread messages. The observed five-message
    burst spanned about 15 minutes.
    """
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
    """Take `target`'s wake reservation and return its id, or None when a current one already
    covers this message. Call under `peers.inbox_guard(root, target)`, after the append.

    Either way the reservation's `watermark` becomes the recipient's arrival count, peer
    messages and dispatch notices together, so it covers everything appended so far.
    A reservation `peers.marked_stale` names was drained by a reader whose acknowledgment
    failed; it covers nothing unread, so a new one is taken. `timeout` bounds the
    session-record lock."""
    watermark = peers_mod.arrivals(root, target)
    prior = as_dict((sessions_mod.get(root, target) or {}).get(peers_mod.WAKE_FIELD))
    if wake_current(prior) and not peers_mod.marked_stale(root, target, prior):
        if prior.get("watermark") != watermark:
            extended = {**prior, "watermark": watermark}
            sessions_mod.set_field(root, target, peers_mod.WAKE_FIELD, extended, timeout)
        return None
    wake_id = uuid.uuid4().hex
    reservation = {"id": wake_id, "state": "attempting", "at": now().timestamp(), "watermark": watermark}
    sessions_mod.set_field(root, target, peers_mod.WAKE_FIELD, reservation, timeout)
    return wake_id


def settle(root: Path, target: str, wake_id: str, accepted: bool, *, timeout: float | None = None) -> None:
    """Record a ring's outcome on its reservation, unless a newer reservation replaced it."""
    deadline = peers_mod.deadline_after(timeout)
    with peers_mod.inbox_guard(root, target, timeout=timeout):
        current = as_dict((sessions_mod.get(root, target) or {}).get(peers_mod.WAKE_FIELD))
        if current.get("id") == wake_id:
            outcome = {**current, "state": "accepted" if accepted else "failed", "at": now().timestamp()}
            sessions_mod.set_field(root, target, peers_mod.WAKE_FIELD, outcome, peers_mod.time_left(deadline))
