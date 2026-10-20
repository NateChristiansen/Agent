# ruff: noqa
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .records.narrow import as_dict, as_dict_or_none

PRICED_AT = "2026-10-09"
LONG_CONTEXT_INPUT = 272000


@dataclass(frozen=True)
class Rates:
    input: float
    output: float
    cache_read: float
    cache_write: float


PRICE_TABLE: dict[str, tuple[Rates, Rates]] = {
    "gpt-6-astra": (Rates(10, 50, 1, 12.5), Rates(20, 75, 2, 25)),
    "gpt-6-sol": (Rates(2, 10, 0.2, 2.5), Rates(4, 15, 0.4, 5)),
    "gpt-6.1-sol": (Rates(2, 10, 0.1, 2.5), Rates(4, 15, 0.2, 5)),
    "gpt-6-luna": (Rates(0.1, 0.5, 0.01, 0.125), Rates(0.2, 0.75, 0.02, 0.25)),
    "gpt-5.6-sol": (Rates(4, 20, 0.4, 5), Rates(8, 30, 0.8, 10)),
}


def require_price(model: str | None) -> None:
    from .core import AgentError

    if not model:
        raise AgentError("Codex dispatch needs a concrete target; name a model or a row")
    if model not in PRICE_TABLE:
        raise AgentError(
            f"Codex model {model!r} has no verified price in src/agent_cli/prices.py; use the model-integration skill to add published rates before launch"
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
    return (input_tokens, output_tokens, cached, written)


def price_usage(model: str, raw: Any) -> float | None:
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
    ) / 1000000


def price_rollout(path: Path) -> float | None:
    models: dict[str, str] = {}
    usage_rows: dict[str, tuple[str, dict[str, object]]] = {}
    cumulative: tuple[int, int, int, int] | None = None
    thread_total: tuple[int, int, int, int] | None = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row = as_dict(row)
                payload = as_dict_or_none(row.get("payload"))
                if payload is None:
                    continue
                if row.get("type") == "turn_context":
                    turn_id, model = (payload.get("turn_id"), payload.get("model"))
                    if isinstance(turn_id, str) and isinstance(model, str):
                        models[turn_id] = model
                elif row.get("type") == "token_usage_record":
                    response_id, turn_id, usage = (
                        payload.get("response_id"),
                        payload.get("turn_id"),
                        as_dict_or_none(payload.get("usage")),
                    )
                    if isinstance(response_id, str) and isinstance(turn_id, str) and (usage is not None):
                        usage_rows[response_id] = (turn_id, usage)
                        thread_total = _usage(payload.get("thread_token_usage"))
                elif row.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = as_dict_or_none(payload.get("info"))
                    if info is not None:
                        cumulative = _usage(info.get("total_token_usage"))
    except OSError:
        return None
    if not usage_rows:
        return None
    totals = [0, 0, 0, 0]
    dollars = 0.0
    for turn_id, raw in usage_rows.values():
        counts = _usage(raw)
        priced = price_usage(models.get(turn_id, ""), raw)
        if counts is None or priced is None:
            return None
        for index, count in enumerate(counts):
            totals[index] += count
        dollars += priced
    if thread_total is not None and tuple(totals) != thread_total:
        return None
    if cumulative is not None and any(
        (count < previous for count, previous in zip(totals, cumulative, strict=True))
    ):
        return None
    return round(dollars, 4)


CLAUDE_RATES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.2),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.5),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.5),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.1),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.2),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.3),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 0.1),
    "claude-haiku-5-5": (0.1, 0.5, 0.125, 0.01),
}
CLAUDE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
HAIKU_LONG_CONTEXT_INPUT = 100000
HAIKU_LONG_RATES = (0.5, 2.5, 0.625, 0.05)


def price_claude_usage(model: str, usage: Any) -> float | None:
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
    if model == "claude-haiku-5-5" and sum((values[0], values[2], values[3])) > HAIKU_LONG_CONTEXT_INPUT:
        rates = HAIKU_LONG_RATES
    return sum((count * rate for count, rate in zip(values, rates, strict=True))) / 1000000
