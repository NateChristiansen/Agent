"""Deliver one durable message with a structured retry outcome. Persist the fact before waking its
recipient. Wake failure never rolls back the mailbox entry, and transport acceptance never proves
the message was read.

Accepted and coalesced-accepted rings complete delivery attempts; consumed messages also complete
them. An unrung self-message is complete, while policy-deferred wakes are not. Coalesced-pending
and refused outcomes remain retryable: a reservation still attempting can fail. Never append the
same durable message twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import peers as peers_mod
from .. import sessions as sessions_mod
from ..core import AgentError
from ..records.narrow import as_dict
from . import peer_wake, peer_wake_policy

COMPLETE = ("accepted", "coalesced_accepted", "consumed")


@dataclass(frozen=True)
class Delivery:
    id: str | None
    wake_id: str | None
    rang: str
    detail: str
    to: str
    queued: dict[str, Any] = field(default_factory=lambda: {})

    @property
    def complete(self) -> bool:
        return self.rang in COMPLETE or (self.rang == "unrung" and self.detail == SELF)

    def notice(self) -> dict[str, Any]:
        """The state a caller stores and passes back as `notice` on its retry."""
        return {"to": self.to, "id": self.id, "wake_id": self.wake_id, "rang": self.rang}


SELF = "the recipient is this session; it reads this at its next turn"


def _reservation(root: Path, target: str) -> dict[str, Any]:
    return as_dict((sessions_mod.get(root, target) or {}).get(peers_mod.WAKE_FIELD))


def deliver(
    root: Path,
    *,
    target: str,
    frm: str,
    message: str,
    notice: dict[str, Any] | None = None,
    policy: bool = False,
) -> Delivery:
    """Queue `message` for `target` unless `notice` shows it is already queued, then ring it
    unless a ring already covers it. `policy` applies the wake policy (`peer_send`); a notice
    passes False, since a landing always warrants a ring.

    Raises only when nothing was queued (an `AgentError` or `OSError` from the append); the
    caller records that as a notice with no id, which the next retry enqueues. A failure after
    the append (reserving, ringing, settling) returns `refused` with the queued id, so the retry
    re-rings that row instead of appending a second one."""
    state: dict[str, Any] = {"id": None, "queued": {}}
    retry = bool(as_dict(notice).get("id"))
    try:
        result = _deliver(root, target, frm, message, as_dict(notice), policy, state)
    except (AgentError, OSError) as exc:
        if not state["id"]:
            raise
        result = Delivery(
            state["id"], None, "refused", f"queued, but the ring failed ({exc})", target, state["queued"]
        )
        state["decided"] = True
    if state.get("decided"):
        _ring_event(root, frm, result, retry)
    return result


def _ring_event(root: Path, frm: str, result: Delivery, retry: bool) -> None:
    """`peer.ring` for a ring decision, joined to its message by id and to its wake by
    reservation id. Acceptance only: it never says the message was read."""
    from ..telemetry import attention as events

    events.emit(
        root,
        "peer.ring",
        session=frm,
        to=result.to,
        wake_id=result.wake_id,
        message_ids=[result.id] if result.id else [],
        outcome=result.rang,
        retry=retry,
    )


def _deliver(
    root: Path,
    target: str,
    frm: str,
    message: str,
    prior: dict[str, Any],
    policy: bool,
    state: dict[str, Any],
) -> Delivery:
    from .peers import wake_peer

    target_record = sessions_mod.get(root, target) or {}
    name = str(target_record.get("name") or target)
    sender = sessions_mod.get(root, frm) or {}
    queued: dict[str, Any] = {}
    with peers_mod.inbox_guard(root, target):
        msg_id = str(prior.get("id") or "") or None
        state["id"] = msg_id
        if msg_id is not None:
            unread = {str(row.get("id")) for row in peers_mod.peek(root, target)}
            if msg_id not in unread:
                return Delivery(
                    msg_id, _str_or_none(prior.get("wake_id")), "consumed", "already read", target
                )
            done = _settled(root, target, prior)
            if done is not None:
                return Delivery(msg_id, done[0], done[1], "", target)
        else:
            queued = peers_mod.send(
                root, to=target, text=message, frm=frm, frm_name=str(sender.get("name") or "")
            )
            msg_id = str(queued["id"])
            state.update(id=msg_id, queued=queued)
        state["decided"] = True  # from here every return is a ring decision
        if target == frm:
            return Delivery(msg_id, None, "unrung", SELF, target, queued)
        if policy:
            wake_id, unrung = peer_wake_policy.reserve(root, target, frm, message)
        else:
            peer_wake_policy.clear(root, target)
            wake_id, unrung = peer_wake.reserve(root, target), ""
        if wake_id is None:
            if unrung:
                return Delivery(msg_id, None, "unrung", unrung, target, queued)
            covering = _reservation(root, target)
            rang = "coalesced_accepted" if covering.get("state") == "accepted" else "coalesced_pending"
            detail = f"{name} already has a pending wake request"
            return Delivery(msg_id, str(covering.get("id") or "") or None, rang, detail, target, queued)
    accepted, detail = wake_peer(target_record, name)
    try:
        peer_wake.settle(root, target, wake_id, accepted)
    except (AgentError, OSError) as exc:  # the ring's own outcome stands; only its record failed
        detail = f"{detail}; recording the ring failed ({exc})"
    return Delivery(msg_id, wake_id, "accepted" if accepted else "refused", detail, target, queued)


def _str_or_none(value: object) -> str | None:
    return str(value) if value else None


def _settled(root: Path, target: str, prior: dict[str, Any]) -> tuple[str | None, str] | None:
    """For a notice whose row is still unread: `(wake_id, rang)` when no new ring is due, or None
    to ring again. Call under the target's inbox guard."""
    rang = str(prior.get("rang") or "")
    if rang in ("accepted", "coalesced_accepted"):
        return _str_or_none(prior.get("wake_id")), rang
    if rang == "coalesced_pending":
        covering = _reservation(root, target)
        if covering.get("id") and covering.get("id") == prior.get("wake_id"):
            if covering.get("state") == "accepted":
                return str(covering["id"]), "coalesced_accepted"
            if covering.get("state") == "attempting" and peer_wake.wake_current(covering):
                return str(covering["id"]), "coalesced_pending"
    return None  # refused, never rung, or its covering wake failed, expired or was replaced
