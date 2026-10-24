"""Track streamed worker token volume while cost is unavailable. Provider cost arrives with the final
result; intermediate zero cost is not evidence of free work. Count assistant usage and retain final
provider dollars separately.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

from ..prices import CLAUDE_FIELDS as FIELDS  # input, output and the two cache halves
from ..prices import price_claude_usage
from ..records.narrow import as_dict, as_int, as_list

# how often the running count reaches the record. Every assistant message would be a
# read-modify-write of the record; the Workers view needs only periodic updates.
FLUSH_SECONDS = 15.0


def message_tokens(usage: Any) -> int:
    """The four counts on one `AssistantMessage.usage`, summed. Anything else reads as 0: the
    field is optional on the SDK's message and absent on an older CLI."""
    counts = as_dict(usage)
    return sum(as_int(counts.get(field)) for field in FIELDS)


def initial_state(d: dict[str, Any]) -> dict[str, Any]:
    """The reader's count at attempt start: what earlier attempts already counted, tokens and
    the request ids behind them."""
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
    """Add one request's tokens to the reader's running count and, every `FLUSH_SECONDS`, write
    the total to the record. `usage` is None for the flush at the end of the stream.

    One API request streams several assistant messages that repeat the same usage, so a
    request is counted once, keyed by `request_id` (the SDK message's `message_id`). The ids
    counted so far live in `state["requests"]` and reach the record, so a resumed attempt does
    not count them again. A message without an id is counted.

    Never raises: a failed write must not take the reader down mid-run."""
    from .supervisor import persist_tokens

    requests: set[str] = state.setdefault("requests", set())
    if usage is not None and not (request_id and request_id in requests):
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
