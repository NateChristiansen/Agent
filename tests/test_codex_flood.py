"""A Codex worker that streams a whitespace flood is stopped mid-stream."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, prices
from agent_cli.dispatch import codex_flood, codex_transport, state
from dispatch_helpers import _new_fast, droot, settle  # noqa: F401

evaluate_mod = importlib.import_module("agent_cli.dispatch.evaluate")


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

    def __init__(self, tool_output: bool = False, answer: bool = False):
        self.process: Any = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[str] = []
        self.tool_output = tool_output
        self.stop = asyncio.Event()
        self.answer = answer
        self.prompt = ""
        self.cwd = ""

    def stderr_tail(self) -> str:
        return ""

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "thread/start":
            return {"thread": {"id": "flood-thread"}}
        if method == "turn/start":
            self.prompt = params["input"][0]["text"]
            self.cwd = params["cwd"]
            asyncio.create_task(self.play())
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            self.stop.set()
        return {}

    async def play(self) -> None:
        await self.events.put(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "last": {"inputTokens": 10, "outputTokens": 10},
                        "total": {"totalTokens": 20},
                    }
                },
            }
        )
        if self.answer:
            nonce = re.search(r"nonce: (\S+)", self.prompt)
            assert nonce is not None
            ret = {
                "nonce": nonce[1],
                "status": "done",
                "summary": "answered after restart",
                "open_questions": [],
                "next_steps": [],
                "notes": "",
            }
            await self.events.put(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "id": "answer",
                            "text": json.dumps(ret),
                        }
                    },
                }
            )
            await self.events.put({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
            return
        if self.tool_output:  # a large tool result is not agent-message text
            for _ in range(50):
                await self.events.put(
                    {"method": "item/commandExecution/outputDelta", "params": {"delta": " " * 4096}}
                )
        while not self.stop.is_set():
            await self.events.put({"method": "item/agentMessage/delta", "params": {"delta": " " * 64}})
            await asyncio.sleep(0)


def _codex(monkeypatch: pytest.MonkeyPatch, rpcs: list[FloodRPC]) -> None:
    attempts = iter(rpcs)

    @asynccontextmanager
    async def server(*_args: Any, **_kwargs: Any):
        yield next(attempts), {}

    monkeypatch.setattr(codex_transport, "isolated_server", server)
    monkeypatch.setattr(prices, "price_usage", lambda *_args: 0.0)


@pytest.mark.parametrize("tool_output", [False, True])
def test_two_floods_are_interrupted_and_only_restarted_once(
    droot: Path, monkeypatch: pytest.MonkeyPatch, tool_output: bool
) -> None:
    monkeypatch.setattr(codex_flood, "WHITESPACE_RUN_LIMIT", 20_000)
    rpc = FloodRPC(tool_output=tool_output)
    second = FloodRPC()
    _codex(monkeypatch, [rpc, second])
    started = time.monotonic()
    result = _new_fast(droot, provider="codex", model="gpt-6-sol", timeout_minutes=2)
    assert time.monotonic() - started < 60, "stopped by the flood bound, not the timeout"
    assert "turn/interrupt" in rpc.calls
    assert result["state"] == "blocked"
    assert "degenerate output" in str(result.get("error"))
    record = state.load(droot, result["id"])
    assert record["attempt"] == 2
    assert [row["reason"] for row in record["auto_resumes"]] == ["degenerate_output"]
    assert "thread/start" in second.calls and "thread/resume" not in second.calls
    assert record["last_result"]["terminal_reason"] == "degenerate_output"
    assert record["result"]["next_steps"] == ["launch the task fresh, or use the other provider"]
    err = state.file(droot, result["id"], ".err").read_text(encoding="utf-8")
    assert "degenerate_output" in err and len(err) < 6 * codex_flood.SAMPLE_BYTES + 2000


def test_flood_then_answer_restarts_with_full_order_in_same_checkout(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = FloodRPC(), FloodRPC(answer=True)
    _codex(monkeypatch, [first, second])
    monkeypatch.setattr(prices, "price_usage", lambda *_args: 0.2)
    result = _new_fast(droot, provider="codex", model="gpt-6-sol", budget=1)
    assert result["state"] == "done" and result["attempt"] == 2
    assert "thread/start" in second.calls and "thread/resume" not in second.calls
    assert first.cwd == second.cwd == result["worktree"]
    assert first.prompt in second.prompt
    assert result["budget_usd"] == 1 and result["max_attempts"] == 2
    assert result["cost_usd"] == pytest.approx(0.4)
    assert [row["cost_usd"] for row in result["thread"] if row["from"] == "worker"] == [0.2, 0.2]
    assert len(result["auto_resumes"]) == 1


def test_flood_retry_is_not_reset_by_manual_resume(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rpcs = [FloodRPC(), FloodRPC(), FloodRPC()]
    _codex(monkeypatch, rpcs)
    result = _new_fast(droot, provider="codex", model="gpt-6-sol")
    settle(droot, result)
    result = dispatch.resume(droot, dispatch_id=result["id"], answer="try again", watch=True)
    assert result["state"] == "blocked" and result["attempt"] == 3
    assert len(result["auto_resumes"]) == 1
    assert "thread/start" in rpcs[2].calls and "thread/resume" not in rpcs[2].calls


@pytest.mark.parametrize("limit", ["attempts", "budget", "attempt_budget"])
def test_flood_does_not_retry_an_exhausted_limit(
    droot: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    _codex(monkeypatch, [FloodRPC()])
    result = _new_fast(droot, provider="codex", model="gpt-6-sol", max_attempts=1)
    assert result["state"] == "blocked" and result["attempt"] == 1
    if limit == "budget":
        result["max_attempts"] = 3
        result["cost_usd"] = result["budget_usd"]
        result["cost_accounted_attempt"] = result["attempt"]
        result["cost_accounting"] = "n1"
    if limit == "attempt_budget":
        result["max_attempts"] = 2
        result["cost_usd"] = 0
        result["cost_accounted_attempt"] = None
        result["cost_accounting"] = "n1"
        result["last_result"]["total_cost_usd"] = result["budget_usd"]
        result["last_result"]["cost_source"] = "local-estimate"
    assert evaluate_mod.evaluate(droot, result)["auto_resume"] is None
