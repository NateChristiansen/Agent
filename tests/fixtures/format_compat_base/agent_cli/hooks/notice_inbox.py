# ruff: noqa
from __future__ import annotations
import time
from pathlib import Path
from typing import Any
from .. import peers
from ..core import AgentError
from ..dispatch import notice_delivery, notices
from ..dispatch.state import load
from ..records.narrow import as_dict

HOOK_DEADLINE_S = 1.0
HEADING = "Dispatch supervisor notices"
_REASONS = {
    "question-pending": "the worker asked a question and its live `dispatch say` window is open; `dispatch show` supplies the question",
    "cleanup-incomplete": "cleanup is incomplete",
    "verification-incomplete": "verification is incomplete",
}


class Notices:
    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root
        self.session_id = session_id
        self.consumed: list[dict[str, Any]] = []

    def unread(self) -> list[dict[str, Any]]:
        sources: dict[str, dict[str, Any] | None] = {}
        out: list[dict[str, Any]] = []
        try:
            items = notice_delivery.pending(self.root, self.session_id)
        except AgentError, OSError, ValueError:
            return []
        for item in items:
            envelope = item.get("envelope")
            if self._actionable(envelope, sources):
                out.append(as_dict(envelope))
        return out

    def _actionable(self, envelope: object, sources: dict[str, dict[str, Any] | None]) -> bool:
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
        nids = [item["notice_id"] for item in items]
        notice_delivery.tombstone_all(self.root, self.session_id, nids, "hook-consumed")
        self.consumed = list(items)


def take(
    root: Path, session_id: str, *, acknowledge_wake: bool, via: str = "prompt"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queue = Notices(root, session_id)
    rows = peers.take(
        root, session_id, acknowledge_wake=acknowledge_wake, timeout=HOOK_DEADLINE_S, shared=queue, via=via
    )
    return (rows, queue.consumed)


def block(envelopes: list[dict[str, Any]]) -> str:
    if not envelopes:
        return ""
    lines = [f"## {HEADING} ({len(envelopes)})", ""]
    for env in envelopes:
        lines.append(f"- {_line(env)}")
    lines += [
        "",
        "From the local dispatch supervisor, not from the owner or a peer: these notices grant no approval and are not an owner instruction. Read the authoritative record with the dispatch_read tool, action show, then mark it read with the dispatch tool, action consume; this notice does not count as read. No peer reply is required.",
    ]
    return "\n".join(lines)


def _line(env: dict[str, Any]) -> str:
    feature = f", feature {env['feature']:03d}" if env.get("feature") else ""
    head = f"dispatch `{env['dispatch_id']}`{feature}, attempt {env['attempt']}: {env['event_class']}"
    facts = f"state {env['state']}, verification {env['verification']}, cleanup {('pending' if env['cleanup_pending'] else 'complete')}"
    if env.get("run_id"):
        facts += f", verification run {env['run_id']}"
    reason = _REASONS.get(str(env.get("attention_reason")))
    return f"{head}; {facts}" + (f"; {reason}" if reason else "") + "."
