"""API-equivalent USD estimates for Claude and Codex token usage.

The harness's one price source. Codex uses OpenAI Standard processing rates per million tokens,
checked 2026-10-09 at https://developers.openai.com/api/docs/pricing; Claude uses Anthropic's
standard first-party rates checked 2026-10-24 (below). These are estimates, not subscription
charges, and an exact provider result replaces them. An absent model or incomplete usage is unknown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .records.narrow import as_dict, as_dict_or_none

PRICED_AT = "2026-10-27"
LONG_CONTEXT_INPUT = 272_000


@dataclass(frozen=True)
class Rates:
    input: float
    output: float
    cache_read: float
    cache_write: float


# Standard short-context and long-context rates, per million tokens. Unknown model IDs stay
# unknown; a prefix match would silently assign a future model an old model's price.
PRICE_TABLE: dict[str, tuple[Rates, Rates]] = {
    "gpt-6-astra": (Rates(10, 50, 1, 12.5), Rates(20, 75, 2, 25)),
    "gpt-6-sol": (Rates(2, 10, 0.2, 2.5), Rates(4, 15, 0.4, 5)),
    "gpt-6.1-sol": (Rates(2, 10, 0.1, 2.5), Rates(4, 15, 0.2, 5)),
    # Standard rates checked 2026-10-27: https://developers.openai.com/api/docs/models/gpt-6.1-terra
    # Cache writes use 30-minute retention ($1.25); the published long-context rule doubles
    # input and cache rates above 272K input tokens, including writes ($2.50).
    "gpt-6.1-terra": (Rates(1, 5, 0.05, 1.25), Rates(2, 7.5, 0.1, 2.5)),
    "gpt-6-luna": (Rates(0.1, 0.5, 0.01, 0.125), Rates(0.2, 0.75, 0.02, 0.25)),
    "gpt-5.6-sol": (Rates(4, 20, 0.4, 5), Rates(8, 30, 0.8, 10)),
}


def require_price(model: str | None) -> None:
    """Refuse an unaccountable Codex target before allocating or starting a worker."""
    from .core import AgentError

    if not model:
        raise AgentError("Codex dispatch needs a concrete target; name a model or a row")
    if model not in PRICE_TABLE:
        raise AgentError(
            f"Codex model {model!r} has no verified price in src/agent_cli/prices.py; "
            "use the model-integration skill to add published rates before launch"
        )


def _usage(raw: object) -> tuple[int, int, int, int] | None:
    usage = as_dict_or_none(raw)
    if usage is None or "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    values: list[int] = []
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens"):
        value = usage.get(key, 0)
        if type(value) is not int or value < 0:
            return None
        values.append(value)
    input_tokens, output_tokens, cached, written = values
    if cached + written > input_tokens:
        return None
    return input_tokens, output_tokens, cached, written


def price_usage(model: str, raw: Any) -> float | None:
    """Price one response. Input includes the cached and cache-write portions.

    Measured in a 2026-09-23 Codex rollout: input=24,200, cached=12,288,
    output=140, total=24,340. Total equals input+output, so cached is a subset of input.
    """
    counts = _usage(raw)
    rates = PRICE_TABLE.get(model)
    if counts is None or rates is None:
        return None
    input_tokens, output_tokens, cached, written = counts
    rate = rates[1 if input_tokens > LONG_CONTEXT_INPUT else 0]
    fresh = input_tokens - cached - written
    return (
        fresh * rate.input
        + cached * rate.cache_read
        + written * rate.cache_write
        + output_tokens * rate.output
    ) / 1_000_000


def price_rollout(path: Path, *, children: list[Path] | None = None) -> float | None:
    """Price a root and its children once per response, using parent rates for guardians."""
    rollouts: list[_Rollout] = []
    for source in [path, *(children or [])]:
        rollout = _read_rollout(source)
        if rollout is None or (source == path and not rollout.usage_rows):
            return None
        rollouts.append(rollout)
    threads = {rollout.ident: rollout for rollout in rollouts if rollout.ident}

    def model_for(rollout: _Rollout, visiting: set[str]) -> str:
        model = next(reversed(rollout.models.values()), "")
        if rollout.guardian and model == "codex-auto-review":
            parent = threads.get(rollout.parent)
            if parent is None or rollout.ident in visiting:
                return ""
            return model_for(parent, visiting | {rollout.ident})
        return model

    seen: set[str] = set()
    dollars = 0.0
    for rollout in rollouts:
        parent = threads.get(rollout.parent)
        parent_model = model_for(parent, set()) if parent is not None else ""
        priced = _price_rollout(rollout, seen, parent_model)
        if priced is None:
            return None
        dollars += priced
    return round(dollars, 4)


@dataclass
class _Rollout:
    ident: str
    parent: str
    guardian: bool
    models: dict[str, str]
    usage_rows: dict[str, tuple[str, dict[str, object]]]
    cumulative: tuple[int, int, int, int] | None
    thread_total: tuple[int, int, int, int] | None


def _read_rollout(path: Path) -> _Rollout | None:
    """Price a Codex rollout by response, checking coverage against its cumulative total.

    Each `token_usage_record` has the response's usage and a turn ID; `turn_context` gives that
    turn's model. The latest `thread_token_usage` proves no responses were missed. Older
    rollouts may only have `token_count.info.total_token_usage`, which can lag one response;
    that value is a lower bound. A cumulative count alone cannot assign models or long-context
    rates accurately.
    """
    models: dict[str, str] = {}
    usage_rows: dict[str, tuple[str, dict[str, object]]] = {}
    cumulative: tuple[int, int, int, int] | None = None
    thread_total: tuple[int, int, int, int] | None = None
    guardian = False
    ident = parent = ""
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a live rollout may end in a partially written line
                row = as_dict(row)
                payload = as_dict_or_none(row.get("payload"))
                if payload is None:
                    continue
                if row.get("type") == "session_meta":
                    guardian = payload.get("thread_source") == "guardian_review"
                    spawn = as_dict(
                        as_dict(as_dict(payload.get("source")).get("subagent")).get("thread_spawn")
                    )
                    ident = str(payload.get("id") or "")
                    parent = str(payload.get("parent_thread_id") or spawn.get("parent_thread_id") or "")
                elif row.get("type") == "turn_context":
                    turn_id, model = payload.get("turn_id"), payload.get("model")
                    if isinstance(turn_id, str) and isinstance(model, str):
                        models[turn_id] = model
                elif row.get("type") == "token_usage_record":
                    response_id, turn_id, usage = (
                        payload.get("response_id"),
                        payload.get("turn_id"),
                        as_dict_or_none(payload.get("usage")),
                    )
                    if isinstance(response_id, str) and isinstance(turn_id, str) and usage is not None:
                        usage_rows[response_id] = (turn_id, usage)
                        thread_total = _usage(payload.get("thread_token_usage"))
                elif row.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = as_dict_or_none(payload.get("info"))
                    if info is not None:
                        cumulative = _usage(info.get("total_token_usage"))
    except OSError, UnicodeError:
        return None
    return _Rollout(ident, parent, guardian, models, usage_rows, cumulative, thread_total)


def _price_rollout(rollout: _Rollout, seen: set[str], parent_model: str) -> float | None:
    if not rollout.usage_rows:
        return 0.0
    totals = [0, 0, 0, 0]
    dollars = 0.0
    for response_id, (turn_id, raw) in rollout.usage_rows.items():
        counts = _usage(raw)
        model = rollout.models.get(turn_id, "")
        if rollout.guardian and model == "codex-auto-review":
            model = parent_model
        priced = price_usage(model, raw)
        if counts is None or priced is None:
            return None
        for index, count in enumerate(counts):
            totals[index] += count
        if response_id not in seen:
            dollars += priced
            seen.add(response_id)
    if rollout.thread_total is not None and tuple(totals) != rollout.thread_total:
        return None
    if rollout.cumulative is not None and any(
        count < previous for count, previous in zip(totals, rollout.cumulative, strict=True)
    ):
        return None
    return dollars


# Standard first-party Claude USD per million tokens: input, output, 5m write, read, 1h write.
# Checked 2026-10-24 at https://platform.claude.com/docs/en/about-claude/pricing.
# Unknown models stay unknown.
CLAUDE_RATES = {
    "claude-fable-5-1": (10.0, 50.0, 12.5, 0.25, 20.0),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.2, 8.0),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.5, 10.0),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.5, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.1, 4.0),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.2, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.3, 6.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 0.1, 2.0),
    "claude-haiku-5-5": (0.1, 0.5, 0.125, 0.01, 0.2),
}
CLAUDE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
HAIKU_LONG_CONTEXT_INPUT = 100_000
HAIKU_LONG_RATES = (0.5, 2.5, 0.625, 0.05, 1.0)


def price_claude_usage(model: str, usage: Any) -> float | None:
    """One Claude request's estimate. Its ordinary input and two cache counts are disjoint."""
    rates = CLAUDE_RATES.get(model)
    counts = as_dict_or_none(usage)
    if rates is None or counts is None:
        return None
    values: list[int] = []
    for field in CLAUDE_FIELDS:
        value = counts.get(field, 0)
        if type(value) is not int or value < 0:
            return None
        values.append(value)
    # The tier applies to the whole request, including output, when the combined input is
    # over 100K.
    if model == "claude-haiku-5-5" and sum((values[0], values[2], values[3])) > HAIKU_LONG_CONTEXT_INPUT:
        rates = HAIKU_LONG_RATES
    written_1h = 0
    if "cache_creation" in counts:
        creation = as_dict_or_none(counts["cache_creation"])
        if creation is None:
            return None
        written_5m = creation.get("ephemeral_5m_input_tokens", 0)
        written_1h = creation.get("ephemeral_1h_input_tokens", 0)
        if (
            type(written_5m) is not int
            or type(written_1h) is not int
            or written_5m < 0
            or written_1h < 0
            or written_5m + written_1h != values[2]
        ):
            return None
        values[2] = written_5m
    values.append(written_1h)
    return sum(count * rate for count, rate in zip(values, rates, strict=True)) / 1_000_000


def price_claude_session(transcript: Path) -> float | None:
    """Price distinct requests across a session and its subagents, retaining maximum output."""
    requests: dict[str, tuple[str, dict[str, Any]]] = {}
    try:
        paths = [transcript, *sorted((transcript.parent / transcript.stem / "subagents").glob("*.jsonl"))]
        for path in paths:
            with path.open("rb") as stream:
                for line in stream:
                    try:
                        row = as_dict(json.loads(line.decode("utf-8")))
                    except ValueError:
                        continue
                    request_id = row.get("requestId")
                    message = as_dict(row.get("message"))
                    model = message.get("model")
                    usage = as_dict_or_none(message.get("usage"))
                    if (
                        row.get("type") != "assistant"
                        or not isinstance(request_id, str)
                        or not request_id
                        or model == "<synthetic>"
                        or usage is None
                    ):
                        continue
                    if not isinstance(model, str) or price_claude_usage(model, usage) is None:
                        return None
                    previous = requests.get(request_id)
                    if previous is None:
                        requests[request_id] = (model, usage)
                    else:
                        previous[1]["output_tokens"] = max(
                            previous[1].get("output_tokens", 0), usage.get("output_tokens", 0)
                        )
    except OSError:
        return None
    if not requests:
        return None
    dollars = 0.0
    for model, usage in requests.values():
        priced = price_claude_usage(model, usage)
        if priced is None:
            return None
        dollars += priced
    return round(dollars, 4)
