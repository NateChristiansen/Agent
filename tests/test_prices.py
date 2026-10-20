"""Codex pricing uses response evidence and keeps gaps unknown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import prices, sessions


def test_price_usage_separates_cache_and_long_context() -> None:
    assert (
        prices.price_usage(
            "gpt-6-sol",
            {
                "input_tokens": 1000,
                "cached_input_tokens": 400,
                "cache_write_input_tokens": 100,
                "output_tokens": 200,
            },
        )
        == 0.00333
    )
    assert (
        prices.price_usage(
            "gpt-6-sol",
            {
                "input_tokens": 300_000,
                "cached_input_tokens": 100_000,
                "cache_write_input_tokens": 0,
                "output_tokens": 1000,
            },
        )
        == 0.855
    )
    assert prices.price_usage("unknown-model", {"input_tokens": 1, "output_tokens": 1}) is None


@pytest.mark.parametrize("input_tokens, expected", [(272_000, 0.3595), (272_001, 0.716504)])
def test_sol_6_1_rates_at_context_boundary(input_tokens: int, expected: float) -> None:
    assert prices.price_usage(
        "gpt-6.1-sol",
        {
            "input_tokens": input_tokens,
            "cached_input_tokens": 100_000,
            "cache_write_input_tokens": 1_000,
            "output_tokens": 500,
        },
    ) == pytest.approx(expected)
    assert prices.price_usage("gpt-6.2-sol", {"input_tokens": 1, "output_tokens": 1}) is None


def test_price_rollout_uses_each_turns_model_and_checks_coverage(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6-sol"}},
        {
            "type": "token_usage_record",
            "payload": {
                "response_id": "r1",
                "turn_id": "t1",
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 100},
                "thread_token_usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 100},
            },
        },
        {"type": "turn_context", "payload": {"turn_id": "t2", "model": "gpt-6-luna"}},
        {
            "type": "token_usage_record",
            "payload": {
                "response_id": "r2",
                "turn_id": "t2",
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 100},
                "thread_token_usage": {"input_tokens": 2000, "cached_input_tokens": 0, "output_tokens": 200},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert prices.price_rollout(path) == 0.0032
    rows[-1]["payload"]["thread_token_usage"]["input_tokens"] = 3000
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert prices.price_rollout(path) is None


def test_unknown_manager_cost_is_not_zero(tmp_path: Path) -> None:
    sessions.mark_cost_unknown(tmp_path, "codex-session", "2026-09-23")
    assert sessions.cost_summary_for_day(tmp_path, "2026-09-23") == (0.0, False, True)
    sessions.record_cost(
        tmp_path,
        "codex-session",
        1.25,
        "2026-09-23",
        source="price-estimate",
        priced_at=prices.PRICED_AT,
    )
    record = sessions.get(tmp_path, "codex-session")
    assert record is not None
    assert record["cost_source"] == "price-estimate"
    assert record["cost_priced_at"] == prices.PRICED_AT
    assert sessions.cost_summary_for_day(tmp_path, "2026-09-23") == (1.25, True, True)
