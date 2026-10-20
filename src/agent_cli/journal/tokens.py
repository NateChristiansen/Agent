"""Format daily manager and subagent token usage from the shared usage collector, scoped by cwd and
deduplicated by request id.
"""

from __future__ import annotations

import re

# input, output, and the two cache halves. `cache_read_input_tokens` is the big one on a long
# session and is priced differently from a fresh input token, so it is kept apart rather than
# folded in.
FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def short_model(model: str) -> str:
    """`claude-opus-5` reads as `opus-5`, `claude-haiku-4-5-20251001` as `haiku-4-5`. The date
    suffix is noise in a one-line summary."""
    name = model.removeprefix("claude-")
    return re.sub(r"-\d{8}$", "", name)


def compact(count: int) -> str:
    """`1_200_000` reads as `1.2M`. Also the status line's token figure for a running worker,
    which is why this is not private."""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def summary(by_model: dict[str, dict[str, int]]) -> str:
    """`opus-5 4.5M, sonnet-5 1.2M`, biggest first. Empty string with nothing to report, so a
    day with no transcripts left on this machine writes no property rather than a zero."""
    rows = sorted(
        ((model, sum(row.get(f, 0) for f in FIELDS)) for model, row in by_model.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return ", ".join(f"{model} {compact(count)}" for model, count in rows if count)
