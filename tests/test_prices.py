"""Provider pricing uses response evidence and keeps gaps unknown."""

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


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-fable-5-1", 92.75),
        ("claude-opus-5-5", 37.2),
        ("claude-opus-5", 46.75),
        ("claude-opus-4-8", 46.75),
        ("claude-sonnet-5-5", 18.6),
        ("claude-sonnet-5", 18.7),
        ("claude-sonnet-4-6", 28.05),
        ("claude-haiku-4-5-20251001", 9.35),
        ("claude-haiku-5-5", 4.675),
    ],
)
def test_claude_published_cache_split(model: str, expected: float) -> None:
    assert prices.price_claude_usage(
        model,
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 2_000_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000_000,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
        },
    ) == pytest.approx(expected)


@pytest.mark.parametrize("written, expected", [(100_000, 0.02), (100_001, 0.100001)])
def test_claude_haiku_cache_split_context_boundary(written: int, expected: float) -> None:
    assert prices.price_claude_usage(
        "claude-haiku-5-5",
        {
            "cache_creation_input_tokens": written,
            "cache_creation": {"ephemeral_1h_input_tokens": written},
        },
    ) == pytest.approx(expected)


@pytest.mark.parametrize(
    "breakdown, expected",
    [
        (None, 0.005),
        ({"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 1}, None),
        ({"ephemeral_5m_input_tokens": -1, "ephemeral_1h_input_tokens": 1001}, None),
        ({"ephemeral_5m_input_tokens": True, "ephemeral_1h_input_tokens": 999}, None),
        ({"ephemeral_5m_input_tokens": 1000.0}, None),
        ("invalid", None),
    ],
)
def test_claude_cache_breakdown_fallback_and_validation(breakdown: object, expected: float | None) -> None:
    usage: dict[str, object] = {"cache_creation_input_tokens": 1000}
    if breakdown is not None:
        usage["cache_creation"] = breakdown
    assert prices.price_claude_usage("claude-opus-5-5", usage) == expected


def _claude_row(request_id: str | None, output: int = 100, model: str = "claude-opus-5-5") -> dict:
    return {
        "type": "assistant",
        "requestId": request_id,
        "message": {"model": model, "usage": {"input_tokens": 1000, "output_tokens": output}},
    }


def test_claude_session_deduplicates_across_subagents_and_keeps_maximum(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(_claude_row("shared", n)) for n in (100, 200, 50)))
    subagents = tmp_path / "session" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                _claude_row("shared", 300),
                _claude_row("shared", 10),
                _claude_row("second", 100, "claude-fable-5-1"),
            ]
        )
    )
    (subagents / "other.jsonl").write_text(json.dumps(_claude_row("second", 50, "claude-fable-5-1")))
    assert prices.price_claude_session(path) == 0.025


def test_claude_session_skips_unusable_rows_and_broken_lines(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    rows = [
        _claude_row(None, model="unknown"),
        _claude_row("", model="unknown"),
        _claude_row("synthetic", model="<synthetic>"),
        {"type": "user", "requestId": "user"},
        {"type": "assistant", "requestId": "missing-message"},
        {"type": "assistant", "requestId": "missing-usage", "message": {"model": "unknown"}},
        [],
    ]
    path.write_bytes(
        "\n".join(json.dumps(row) for row in rows).encode()
        + b'\n{"partial":\n\xff\n'
        + json.dumps(_claude_row("valid")).encode()
    )
    assert prices.price_claude_session(path) == 0.006


def test_claude_session_unknown_or_incomplete_request_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(_claude_row("valid")))
    subagents = tmp_path / "session" / "subagents"
    subagents.mkdir(parents=True)
    subagent = subagents / "agent.jsonl"
    subagent.write_text(json.dumps(_claude_row("unknown", model="claude-future")))
    assert prices.price_claude_session(path) is None
    subagent.write_text(
        json.dumps(
            {
                "type": "assistant",
                "requestId": "invalid",
                "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": -1}},
            }
        )
    )
    assert prices.price_claude_session(path) is None


def test_claude_session_unreadable_or_empty_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    assert prices.price_claude_session(path) is None
    path.write_text("")
    assert prices.price_claude_session(path) is None
    path.write_text(json.dumps(_claude_row("valid")))
    subagents = tmp_path / "session" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "unreadable.jsonl").mkdir()
    assert prices.price_claude_session(path) is None
