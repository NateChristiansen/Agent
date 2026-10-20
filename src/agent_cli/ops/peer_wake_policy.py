"""Which peer messages ring their recipient now, which never ring, and which may wait.

Peer wakes cost about two thirds of the managers' cache-read tokens in the audit week, mostly
rings for messages that needed nothing from the recipient. A message is classified in order:

1. Actionable, ringing now (under the usual per-recipient reservation): a question mark, an
   explicit question or decision kind, or a decision or imperative word ("approve", "proceed",
   "fix", "please", "don't", ...). "Approved. Proceed with landing." rings.
2. Receipt-only, never ringing: the whole message is acknowledgement words ("ack", "thanks",
   "got it", "ok"), nothing else. It is still stored and read at the recipient's next turn, so
   an acknowledgement can never start an acknowledgement loop.
3. Informational: everything else. It rings now while `peers.defer_nonurgent_s` is 0 (the
   default, today's behaviour). With a positive value it is queued unrung, and rung later.

No daemon watches the clock. A deferred message is delivered by the recipient's own next turn
(its prompt and stop hooks drain the inbox), rung by the sender's Stop hook once it has waited
the interval, or carried along by any actionable ring to that recipient. Doctor lists deferrals
older than twice the interval. If neither session takes another turn, an informational message
waits for the recipient's next one: that is what non-urgent means.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import peers as peers_mod
from .. import sessions as sessions_mod
from ..core import now
from ..records.narrow import as_int, as_list, dict_list, dig

ACTIONABLE, RECEIPT, INFORMATIONAL = "actionable", "receipt", "informational"
DEFER_FIELD = peers_mod.DEFER_FIELD
_DECISION = re.compile(
    r"\b(approve|approved|agree|agreed|confirm|proceed|go ahead|land|merge|push|stop|wait|hold|block|"
    r"blocked|fix|retry|revert|review|please|need|must|should|don't|do not)\b",
    re.IGNORECASE,
)
_ACK = r"(ack(nowledged)?|received|noted|thanks|thank you|got it|ok(ay)?|👍)"
_RECEIPT = re.compile(rf"^\s*{_ACK}([\s.,!:-]+{_ACK})*[\s.!]*$", re.IGNORECASE)


def classify(text: str, kind: str | None = None) -> str:
    if "?" in text or kind in ("question", "decision") or _DECISION.search(text):
        return ACTIONABLE
    if _RECEIPT.match(text):
        return RECEIPT
    return INFORMATIONAL


def defer_seconds(root: Path) -> int:
    """`peers.defer_nonurgent_s` from agent.yaml; 0 (ring now) when absent or invalid."""
    import yaml

    from ..config_file import registry_path

    try:
        data = yaml.safe_load(registry_path(root).read_text(encoding="utf-8"))
    except OSError, ValueError, yaml.YAMLError:
        return 0
    return max(as_int(dig(data, "peers", "defer_nonurgent_s")), 0)


def decide(root: Path, text: str) -> tuple[bool, str]:
    """`(ring now, why not)` for one message."""
    kind = classify(text)
    if kind == RECEIPT:
        return False, "an acknowledgement never rings; it is read at the recipient's next turn"
    if kind == INFORMATIONAL and (delay := defer_seconds(root)):
        return False, f"not urgent, so it waits unrung for the recipient's next turn, or {delay} s"
    return True, ""


def reserve(root: Path, target: str, sender: str, text: str) -> tuple[str | None, str]:
    """Under the recipient's inbox guard, after the append: the wake reservation to ring, or None
    and why this message is not rung. A ring carries everything queued, deferrals included."""
    from . import peer_wake

    ring, why = decide(root, text)
    if not ring:
        if classify(text) == INFORMATIONAL:
            defer(root, target, sender)
        return None, why
    clear(root, target)
    return peer_wake.reserve(root, target), ""


def defer(root: Path, target: str, sender: str, *, timeout: float | None = None) -> None:
    """Note an unrung informational message on the recipient's record. Call under its inbox guard."""
    rows = list(as_list((sessions_mod.get(root, target) or {}).get(DEFER_FIELD)))
    rows.append({"from": sender, "at": now().timestamp()})
    sessions_mod.set_field(root, target, DEFER_FIELD, rows, timeout)


def clear(root: Path, target: str, *, timeout: float | None = None) -> None:
    """The recipient read its inbox, or a ring covered it: nothing is deferred any more."""
    if (sessions_mod.get(root, target) or {}).get(DEFER_FIELD):
        sessions_mod.set_field(root, target, DEFER_FIELD, [], timeout)


def ring_expired(root: Path, sender: str) -> list[str]:
    """Ring each recipient holding a deferral from `sender` older than the interval. Returns the
    recipients rung. Never raises."""
    from . import peer_wake
    from . import peers as ops_peers

    delay = defer_seconds(root)
    rung: list[str] = []
    try:
        records = sessions_mod.all_records(root)
    except Exception:  # noqa: BLE001 — a Stop hook never fails on this
        return []
    for record in records:
        target = str(record.get("session_id") or "")
        due = [r for r in dict_list(record.get(DEFER_FIELD)) if r.get("from") == sender and _age(r) >= delay]
        if not target or not due:
            continue
        try:
            with peers_mod.inbox_guard(root, target):
                wake_id = peer_wake.reserve(root, target)
                clear(root, target)
            if wake_id:
                accepted, _ = ops_peers.wake_peer(record, str(record.get("name") or target))
                peer_wake.settle(root, target, wake_id, accepted)
            rung.append(target)
        except Exception:  # noqa: BLE001, S112 — the message stays queued for the next turn
            continue
    return rung


def _age(row: dict[str, Any]) -> float:
    try:
        return now().timestamp() - float(row.get("at") or 0)
    except TypeError, ValueError:
        return 0.0
