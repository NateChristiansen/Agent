# ruff: noqa
from __future__ import annotations
import re

FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def short_model(model: str) -> str:
    name = model.removeprefix("claude-")
    return re.sub("-\\d{8}$", "", name)


def compact(count: int) -> str:
    if count >= 1000000000:
        return f"{count / 1000000000:.1f}B"
    if count >= 1000000:
        return f"{count / 1000000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.0f}k"
    return str(count)


def summary(by_model: dict[str, dict[str, int]]) -> str:
    rows = sorted(
        ((model, sum((row.get(f, 0) for f in FIELDS))) for model, row in by_model.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return ", ".join((f"{model} {compact(count)}" for model, count in rows if count))
