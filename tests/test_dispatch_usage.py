"""A running worker's token count.

Dollars for a dispatch exist once, in the SDK's result message, so a worker half an hour in
needs a token count while running. The reader counts `AssistantMessage.usage` and writes
the total to the record, throttled, for the Workers view.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import prices
from agent_cli.dispatch import usage as usage_mod


def _write_dispatch(root: Path, dispatch_id: str) -> None:
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    record = {"id": dispatch_id, "state": "running", "cost_usd": 0.0, "tokens": 0}
    (base / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _tokens(root: Path, dispatch_id: str) -> int:
    return json.loads((root / ".agent" / "dispatches" / f"{dispatch_id}.json").read_text())["tokens"]


def test_usage_is_the_four_counts_summed():
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 1000,
        "service_tier": "standard",  # not a count; must not blow up
    }
    assert usage_mod.message_tokens(usage) == 1115
    assert usage_mod.message_tokens(None) == 0
    assert usage_mod.message_tokens({"input_tokens": "nonsense"}) == 0


def test_the_running_total_reaches_the_record(tmp_root: Path):
    _write_dispatch(tmp_root, "001-01")
    state: dict[str, object] = {"tokens": 0, "tokens_written": 0.0}
    usage_mod.count(tmp_root, "001-01", {"input_tokens": 400}, state)
    assert _tokens(tmp_root, "001-01") == 400


def test_writes_are_throttled_but_the_count_keeps_climbing(tmp_root: Path):
    """Streamed usage keeps climbing between record writes. Throttling avoids a write per message."""
    _write_dispatch(tmp_root, "001-01")
    state: dict[str, object] = {"tokens": 0, "tokens_written": 0.0}
    usage_mod.count(tmp_root, "001-01", {"input_tokens": 400}, state)
    usage_mod.count(tmp_root, "001-01", {"output_tokens": 600}, state)
    assert state["tokens"] == 1000
    assert _tokens(tmp_root, "001-01") == 400  # second write throttled
    usage_mod.count(tmp_root, "001-01", None, state, force=True)
    assert _tokens(tmp_root, "001-01") == 1000


def test_a_request_streamed_as_several_messages_counts_once(tmp_root: Path):
    _write_dispatch(tmp_root, "001-01")
    state: dict[str, object] = {"tokens": 0, "tokens_written": 0.0}
    usage = {"input_tokens": 10, "output_tokens": 5}
    for _ in range(3):
        usage_mod.count(tmp_root, "001-01", usage, state, model="claude-sonnet-5-5", request_id="msg_1")
    usage_mod.count(tmp_root, "001-01", usage, state, model="claude-sonnet-5-5", request_id="msg_2")
    assert state["tokens"] == 30
    assert state["estimated_cost_usd"] == pytest.approx(2 * (10 * 2.0 + 5 * 10.0) / 1_000_000)


def test_a_resumed_attempt_does_not_count_an_earlier_request_again(tmp_root: Path):
    _write_dispatch(tmp_root, "001-01")
    first: dict[str, object] = {"tokens": 0, "tokens_written": 0.0}
    usage_mod.count(tmp_root, "001-01", {"input_tokens": 400}, first, request_id="msg_1")
    usage_mod.count(tmp_root, "001-01", None, first, force=True)
    record = json.loads((tmp_root / ".agent" / "dispatches" / "001-01.json").read_text())
    assert record["token_requests"] == ["msg_1"]
    second = usage_mod.initial_state(record)
    usage_mod.count(tmp_root, "001-01", {"input_tokens": 400}, second, request_id="msg_1")
    usage_mod.count(tmp_root, "001-01", {"input_tokens": 100}, second, request_id="msg_2", force=True)
    assert _tokens(tmp_root, "001-01") == 500


def test_a_failed_write_does_not_take_the_reader_down(tmp_root: Path):
    """The record may be gone (a cancel finalized it) while the stream is still draining."""
    state: dict[str, object] = {"tokens": 0, "tokens_written": 0.0}
    usage_mod.count(tmp_root, "no-such-dispatch", {"input_tokens": 400}, state)
    assert state["tokens"] == 400


@pytest.mark.parametrize("ordinary, expected", [(1_000, 0.010115), (1_001, 0.0505755)])
def test_haiku_5_5_cache_heavy_input_crosses_tier(ordinary: int, expected: float):
    assert prices.price_claude_usage(
        "claude-haiku-5-5",
        {
            "input_tokens": ordinary,
            "output_tokens": 10_000,
            "cache_creation_input_tokens": 35_000,
            "cache_read_input_tokens": 64_000,
        },
    ) == pytest.approx(expected)


def test_refreshed_sonnet_cache_price_and_unknown_estimate():
    assert prices.price_claude_usage("claude-sonnet-5-5", {"cache_read_input_tokens": 1_000_000}) == 0.1
    assert prices.price_claude_usage("claude-future-9", {"input_tokens": 1_000}) is None
