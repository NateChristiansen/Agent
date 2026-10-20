"""A Codex worker that streams a whitespace flood is stopped mid-stream."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli import prices
from agent_cli.dispatch import codex_flood, codex_transport, state
from dispatch_helpers import _new_fast, droot  # noqa: F401


def test_whitespace_run_spans_deltas_and_resets_on_text() -> None:
    flood = codex_flood.Flood()
    flood.add("answer:   ")
    flood.add("   ")
    assert flood.whitespace_run == 6
    flood.add("x")
    assert flood.whitespace_run == 0
    flood.add(" " * codex_flood.WHITESPACE_RUN_LIMIT)
    assert flood.degenerate()
    assert "whitespace run" in flood.why()
    assert len(flood.head) <= codex_flood.SAMPLE_BYTES and len(flood.tail) <= codex_flood.SAMPLE_BYTES


@pytest.mark.parametrize(
    "deltas",
    [
        ["prefix" + "\n" * codex_flood.WHITESPACE_RUN_LIMIT + "suffix"],  # a run followed by text
        ["\n" * (codex_flood.WHITESPACE_RUN_LIMIT - 1), "\nx"],  # split across two deltas
    ],
)
def test_a_run_is_caught_wherever_it_sits(deltas: list[str]) -> None:
    """Codex review, phase 1: only a trailing run was measured, so both of these passed."""
    flood = codex_flood.Flood()
    verdicts = [flood.take(d) for d in deltas]
    assert verdicts[-1] == "stop"


def test_ordinary_text_is_not_degenerate() -> None:
    flood = codex_flood.Flood()
    for _ in range(2000):
        flood.add("a normal sentence with spaces.\n")
    assert not flood.degenerate()


class FloodRPC:
    """Streams small whitespace deltas forever and never completes the item or the turn."""

    def __init__(self, tool_output: bool = False):
        self.process: Any = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[str] = []
        self.tool_output = tool_output
        self.stop = asyncio.Event()

    def stderr_tail(self) -> str:
        return ""

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "thread/start":
            return {"thread": {"id": "flood-thread"}}
        if method == "turn/start":
            asyncio.create_task(self.play())
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            self.stop.set()
        return {}

    async def play(self) -> None:
        if self.tool_output:  # a large tool result is not agent-message text
            for _ in range(50):
                await self.events.put(
                    {"method": "item/commandExecution/outputDelta", "params": {"delta": " " * 4096}}
                )
        while not self.stop.is_set():
            await self.events.put({"method": "item/agentMessage/delta", "params": {"delta": " " * 64}})
            await asyncio.sleep(0)


def _codex(monkeypatch: pytest.MonkeyPatch, rpc: FloodRPC) -> None:
    @asynccontextmanager
    async def server(*_args: Any, **_kwargs: Any):
        yield rpc, {}

    monkeypatch.setattr(codex_transport, "isolated_server", server)
    monkeypatch.setattr(prices, "price_usage", lambda *_args: 0.0)


@pytest.mark.parametrize("tool_output", [False, True])
def test_a_flood_with_no_completed_item_is_interrupted_and_never_resumed(
    droot: Path, monkeypatch: pytest.MonkeyPatch, tool_output: bool
) -> None:
    monkeypatch.setattr(codex_flood, "WHITESPACE_RUN_LIMIT", 20_000)
    rpc = FloodRPC(tool_output=tool_output)
    _codex(monkeypatch, rpc)
    started = time.monotonic()
    result = _new_fast(droot, provider="codex", model="gpt-6-sol", timeout_minutes=2)
    assert time.monotonic() - started < 60, "stopped by the flood bound, not the timeout"
    assert "turn/interrupt" in rpc.calls
    assert result["state"] == "blocked"
    assert "degenerate output" in str(result.get("error"))
    record = state.load(droot, result["id"])
    assert not record.get("auto_resumes")
    assert record["last_result"]["terminal_reason"] == "degenerate_output"
    assert record["result"]["next_steps"] == ["launch the task fresh, or use the other provider"]
    err = state.file(droot, result["id"], ".err").read_text(encoding="utf-8")
    assert "degenerate_output" in err and len(err) < 3 * codex_flood.SAMPLE_BYTES + 1000
