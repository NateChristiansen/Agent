# ruff: noqa
from __future__ import annotations
import contextlib
import time
from pathlib import Path
from typing import Any
from ..prices import CLAUDE_FIELDS as FIELDS
from ..prices import price_claude_usage
from ..records.narrow import as_dict, as_int, as_list

FLUSH_SECONDS = 15.0


def message_tokens(usage: Any) -> int:
    counts = as_dict(usage)
    return sum((as_int(counts.get(field)) for field in FIELDS))


def initial_state(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "tokens": as_int(d.get("tokens")),
        "tokens_written": 0.0,
        "requests": {str(r) for r in as_list(d.get("token_requests"))},
    }


def count_message(root: Path, dispatch_id: str, msg: Any, state: dict[str, Any]) -> None:
    count(
        root,
        dispatch_id,
        msg.usage,
        state,
        model=str(msg.model or ""),
        request_id=getattr(msg, "message_id", None),
    )


def count(
    root: Path,
    dispatch_id: str,
    usage: Any,
    state: dict[str, Any],
    *,
    model: str = "",
    force: bool = False,
    request_id: str | None = None,
) -> None:
    from .supervisor import persist_tokens

    requests: set[str] = state.setdefault("requests", set())
    if usage is not None and (not (request_id and request_id in requests)):
        if request_id:
            requests.add(request_id)
        state["tokens"] = int(state.get("tokens") or 0) + message_tokens(usage)
        estimate = price_claude_usage(model, usage)
        if estimate is not None:
            state["estimated_cost_usd"] = float(state.get("estimated_cost_usd") or 0.0) + estimate
    if not state.get("tokens"):
        return
    if not force and time.monotonic() - float(state.get("tokens_written") or 0.0) < FLUSH_SECONDS:
        return
    state["tokens_written"] = time.monotonic()
    with contextlib.suppress(Exception):
        persist_tokens(root, dispatch_id, int(state["tokens"]), sorted(requests))
