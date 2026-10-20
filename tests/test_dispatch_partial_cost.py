"""A broken SDK stream retains a labeled estimate from observed usage."""

from pathlib import Path

from agent_cli.dispatch.spend import current_cost
from dispatch_helpers import _new, droot  # noqa: F401 -- pytest fixture used as a parameter


def test_stream_ends_before_result_but_keeps_observed_cost(droot: Path, fake_claude):
    assistant = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "work in progress"}],
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    fake_claude.scenario([{"turns": [{"messages": [assistant], "end": True}]}])
    d = _new(droot)
    assert d["state"] == "failed"
    assert d["cost_usd"] == 3.0
    assert d["cost_estimated_attempts"] == [1]
    assert current_cost(d) == (3.0, "local-estimate")
    assert "local estimate" in (droot / "features/001-alpha-work/log.md").read_text(encoding="utf-8")
