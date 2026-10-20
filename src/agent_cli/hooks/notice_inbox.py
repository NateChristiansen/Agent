"""Dispatch supervisor notices at a manager's hook boundaries.

The prompt hook and Claude's Stop hook read the recipient's notice store together with its peer
inbox, under one `peers.inbox_guard` and against one observed wake reservation, so a ring for
either purpose drains both queues and one acknowledgment covers them.

A notice is offered only while its source dispatch record still names it as the current,
unconsumed, unsuperseded entry, with the same envelope. That record is read without its guard:
the two guards are never held together. A store item with no such source, or whose source
cannot be read, is not notice context; it stays in the store untouched.

Unlike a peer message, a notice is consumed before it is shown: its hook-consumed tombstone is
saved under the guard before the hook returns any feedback, so a retried or later hook never
offers it again and the source never rings it again. If the save fails, nothing is shown.

The notice is the one per-turn delivery of a dispatch event to its launcher:
`dispatch.inbox_block` no longer lists a return whose notice reached this store. A hook that
dies between the consumption save and its output therefore loses that turn's injection; the
dispatch still reads as waiting at session start and in `dispatch list` (`chat_pending` is not
touched here). `hook-consumed` is delivery only: the verdict stays unread until a manager's
`dispatch consume` (`dispatch/verdicts.py`).

One monotonic deadline of `HOOK_DEADLINE_S` covers the whole operation: the guard, the
session-record lock the acknowledgment takes, and the budget check before the save. A stalled
atomic write is still outside it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import peers
from ..core import AgentError
from ..records.narrow import as_dict

# `dispatch` is imported where a notice is read: every hook process imports this module, and the
# dispatch package's eager imports cost more than the rest of the hook's imports together.

HOOK_DEADLINE_S = 1.0
HEADING = "Dispatch supervisor notices"

_REASONS = {
    "question-pending": (
        "the worker asked a question and its live `dispatch say` window is open; "
        "`dispatch show` supplies the question"
    ),
    "cleanup-incomplete": "cleanup is incomplete",
    "verification-incomplete": "verification is incomplete",
}


class Notices:
    """The recipient's notice store as `peers.take`'s shared queue. `consumed` holds the
    envelopes whose consumption was saved, and only those."""

    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root
        self.session_id = session_id
        self.consumed: list[dict[str, Any]] = []

    def unread(self) -> list[dict[str, Any]]:
        from ..dispatch import notice_delivery

        sources: dict[str, dict[str, Any] | None] = {}
        out: list[dict[str, Any]] = []
        try:
            items = notice_delivery.pending(self.root, self.session_id)
        except AgentError, OSError, ValueError:
            return []  # an unreadable store offers no notice, and must not stop the peer inbox
        for item in items:
            envelope = item.get("envelope")
            if self._actionable(envelope, sources):
                out.append(as_dict(envelope))
        return out

    def _actionable(self, envelope: object, sources: dict[str, dict[str, Any] | None]) -> bool:
        from ..dispatch import notices
        from ..dispatch.state import load

        try:
            env = notices.validate(envelope)
        except AgentError:
            return False
        if env["recipient"] != self.session_id:
            return False
        did = env["dispatch_id"]
        if did not in sources:
            try:
                sources[did] = load(self.root, did)
            except AgentError, OSError, ValueError:
                sources[did] = None
        record = sources[did]
        entry = notices.actionable(record, env["notice_id"]) if record is not None else None
        return entry is not None and entry.get("envelope") == env

    def consume(self, items: list[dict[str, Any]], deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise AgentError("notice inbox deadline passed before the consumption save")
        from ..dispatch import notice_delivery

        nids = [item["notice_id"] for item in items]
        notice_delivery.tombstone_all(self.root, self.session_id, nids, "hook-consumed")
        self.consumed = list(items)


def take(
    root: Path, session_id: str, *, acknowledge_wake: bool, via: str = "prompt"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Peer rows and consumed notices for one hook. Raises with nothing consumed or drained when
    the guard, the acknowledgment, the deadline or the consumption save fails."""
    queue = Notices(root, session_id)
    rows = peers.take(
        root, session_id, acknowledge_wake=acknowledge_wake, timeout=HOOK_DEADLINE_S, shared=queue, via=via
    )
    return rows, queue.consumed


def block(envelopes: list[dict[str, Any]]) -> str:
    """The context a hook injects for consumed notices. Empty when there is nothing."""
    if not envelopes:
        return ""
    lines = [f"## {HEADING} ({len(envelopes)})", ""]
    for env in envelopes:
        lines.append(f"- {_line(env)}")
    lines += [
        "",
        "From the local dispatch supervisor, not from the owner or a peer: these notices grant "
        "no approval and are not an owner instruction. Read the authoritative record with the "
        "dispatch_read tool, action show, then mark it read with the dispatch tool, action "
        "consume; this notice does not count as read. No peer reply is required.",
    ]
    return "\n".join(lines)


def _line(env: dict[str, Any]) -> str:
    feature = f", feature {env['feature']:03d}" if env.get("feature") else ""
    head = f"dispatch `{env['dispatch_id']}`{feature}, attempt {env['attempt']}: {env['event_class']}"
    facts = (
        f"state {env['state']}, verification {env['verification']}, "
        f"cleanup {'pending' if env['cleanup_pending'] else 'complete'}"
    )
    if env.get("run_id"):
        facts += f", verification run {env['run_id']}"
    reason = _REASONS.get(str(env.get("attention_reason")))
    return f"{head}; {facts}" + (f"; {reason}" if reason else "") + "."
