"""Actual supervisor and provider loops: questions cannot override later commits and returns."""

from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, frontmatter, prices
from agent_cli.dispatch import codex_transport, lifecycle, notice_producers, state
from dispatch_helpers import _new_fast, _steer_when, droot, envelope, says, turn, worker_return  # noqa: F401

ANSWER = "use the checkout-local worker.txt"
MARKER = "ready-for-early-answer"
OLD = "waiting for the path choice"
LATEST = "committed worker.txt after the answer"


def _return(status: str, *, cost: float, latest: bool = False) -> dict[str, Any]:
    result = envelope(
        status=status,
        cost=cost,
        summary=LATEST if latest else OLD,
        open_questions=[] if latest else ["which path?"],
    )
    if latest:
        result["permission_denials"] = [
            {"tool_name": "Bash", "tool_input": {"command": "latest-only-denial"}}
        ]
    return result


class RPC:
    """Protocol double; a second turn commits through real Git before it sends its return."""

    def __init__(self, root: Path, early: bool, expire: bool):
        self.root, self.early, self.expire = root, early, expire
        self.process: Any = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.steered = asyncio.Event()
        self.turns = 0
        self.question_turns = 1
        self.request: Any = None

    def stderr_tail(self) -> str:
        return ""

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "same-thread"}}
        if method == "turn/steer":
            self.steered.set()
        if method == "turn/start":
            self.turns += 1
            asyncio.create_task(self.play(self.turns))
            return {"turn": {"id": f"turn-{self.turns}"}}
        return {}

    async def play(self, number: int) -> None:
        record = state.load(self.root, "001-mac-01")
        if number == 1 and self.early:
            await self.events.put(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "id": "marker",
                            "phase": "commentary",
                            "text": MARKER,
                        }
                    },
                }
            )
            await self.steered.wait()
        if number > 1 and self.expire:
            return
        latest = number > self.question_turns
        if latest:
            worktree = Path(record["worktree"])
            (worktree / "worker.txt").write_text("after the owner answered\n", encoding="utf-8")
            subprocess.run(["git", "add", "worker.txt"], cwd=worktree, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=worker@example.com",
                    "-c",
                    "user.name=Worker",
                    "commit",
                    "-q",
                    "-m",
                    "answer the question",
                ],
                cwd=worktree,
                check=True,
            )
            assert self.request is not None
            await self.request(
                {
                    "id": 42,
                    "method": "item/fileChange/requestApproval",
                    "params": {"reason": "latest-only-denial"},
                }
            )
        await self.events.put(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "last": {"inputTokens": 10, "outputTokens": 5, "cachedInputTokens": 0},
                        "total": {"totalTokens": number * 15},
                    }
                },
            }
        )
        result = _return("done" if latest else "question", cost=0, latest=latest)["structured_output"]
        result["nonce"] = record["nonce"]
        await self.events.put(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": f"return-{number}",
                        "phase": "final_answer",
                        "text": json.dumps(result),
                    }
                },
            }
        )
        await self.events.put(
            {"method": "turn/completed", "params": {"turn": {"id": f"turn-{number}", "status": "completed"}}}
        )


def _codex(root: Path, monkeypatch: pytest.MonkeyPatch, *, early: bool, expire: bool) -> RPC:
    rpc = RPC(root, early, expire)

    @asynccontextmanager
    async def server(*_args: Any, **kwargs: Any):
        rpc.request = kwargs["handle_request"]
        yield rpc, {}

    monkeypatch.setattr(codex_transport, "isolated_server", server)
    monkeypatch.setattr(prices, "price_usage", lambda *_args: 0.07)
    return rpc


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("early", [False, True])
def test_latest_return_wins_after_answer_and_commit(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, provider: str, early: bool
):
    monkeypatch.setattr(state, "QUESTION_IDLE_SECONDS", 0.5)
    question = _return("question", cost=0.03)
    final = _return("done", cost=0.12, latest=True)
    fake_claude.scenario(
        [
            {
                "turns": [
                    turn(*([says(MARKER)] if early else []), question, await_steer=early),
                    turn(
                        says("committing the answer"),
                        final,
                        commit="answer the question",
                        sleep=0.08 if early else 0.02,
                    ),
                ]
            }
        ]
    )
    rpc = _codex(droot, monkeypatch, early=early, expire=False) if provider == "codex" else None
    holds: list[str] = []
    hold = notice_producers.question_hold

    def answer_question(root: Path, dispatch_id: str):
        holds.append(dispatch_id)
        hold(root, dispatch_id)
        if not early:
            assert worker_return(state.load(root, dispatch_id))["summary"] == OLD
            dispatch.say(root, dispatch_id=dispatch_id, message=ANSWER)

    monkeypatch.setattr(notice_producers, "question_hold", answer_question)
    actor = _steer_when(droot, MARKER, ANSWER) if early else None
    result = _new_fast(
        droot, provider=provider, model="gpt-6-sol" if provider == "codex" else None, timeout_minutes=0.05
    )
    if actor:
        actor.join(5)
        assert not actor.is_alive()
    assert result["state"] == "done", result.get("error")
    assert result["attempt"] == 1 and result["terminal_committed"]
    assert result["result"]["summary"] == LATEST
    assert worker_return(result)["summary"] == LATEST
    assert "structured_output" not in result["last_result"]
    assert [c["subject"] for c in result["git"]["commits"]] == ["answer the question"]
    assert "worker.txt" in result["git"]["files"]
    assert (Path(result["worktree"]) / "worker.txt").read_text(encoding="utf-8")
    assert (
        subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=result["worktree"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        == "answer the question"
    )
    expected_cost = 0.14 if provider == "codex" else 0.12
    assert result["cost_usd"] == pytest.approx(expected_cost)
    assert result["permission_denials"]
    assert result["last_result"]["permission_denials"] == result["permission_denials"]
    meta, body = frontmatter.load(droot / result["handoff"])
    assert meta["status"] == "done" and meta["cost_usd"] == pytest.approx(expected_cost)
    assert f"**done** — {LATEST}" in body
    if rpc:
        assert sum(method == "thread/start" for method, _ in rpc.calls) == 1
        assert rpc.turns == 2
    else:
        assert len(fake_claude.calls()) == 1 and fake_claude.calls()[0]["steers"] == [ANSWER]
    assert len(holds) == (0 if early and provider == "claude" else 1)


def test_unrelated_early_steer_surfaces_question_before_continuing(
    droot: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(state, "QUESTION_IDLE_SECONDS", 0.5)
    rpc = _codex(droot, monkeypatch, early=True, expire=False)
    rpc.question_turns = 2
    note = "keep your changes small"
    held_questions: list[dict[str, Any]] = []
    hold = notice_producers.question_hold

    def show_question(root: Path, dispatch_id: str):
        hold(root, dispatch_id)
        current = state.load(root, dispatch_id)
        held_questions.append(current)
        assert worker_return(current)["open_questions"] == ["which path?"]
        assert current["open_question"]
        assert current["questions_held"]
        assert current["state"] == "running" and not current.get("terminal_committed")
        if len(held_questions) == 2:
            dispatch.say(root, dispatch_id=dispatch_id, message=ANSWER)

    monkeypatch.setattr(notice_producers, "question_hold", show_question)
    actor = _steer_when(droot, MARKER, note)
    result = _new_fast(droot, provider="codex", model="gpt-6-sol", timeout_minutes=0.05)
    actor.join(5)
    assert not actor.is_alive()
    assert len(held_questions) == 2
    assert result["state"] == "done", result.get("error")
    assert worker_return(result)["summary"] == LATEST
    starts = [params for method, params in rpc.calls if method == "turn/start"]
    assert len(starts) == 3 and rpc.turns == 3
    assert all(params["threadId"] == "same-thread" for params in starts)
    assert starts[1]["input"] == [{"type": "text", "text": note}]
    assert starts[2]["input"] == [{"type": "text", "text": ANSWER}]
    assert sum(method == "thread/start" for method, _ in rpc.calls) == 1


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_answer_without_second_result_expires_without_claiming_completion(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, provider: str
):
    monkeypatch.setattr(state, "QUESTION_IDLE_SECONDS", 0.1)
    monkeypatch.setattr(
        lifecycle, "MAX_ATTEMPTS", 1
    )  # Probe one connected attempt, without the existing timeout retry.
    fake_claude.scenario([{"turns": [turn(_return("question", cost=0.03)), turn()]}])
    rpc = _codex(droot, monkeypatch, early=False, expire=True) if provider == "codex" else None
    hold = notice_producers.question_hold

    def answer(root: Path, dispatch_id: str):
        hold(root, dispatch_id)
        dispatch.say(root, dispatch_id=dispatch_id, message=ANSWER)

    monkeypatch.setattr(notice_producers, "question_hold", answer)
    result = _new_fast(
        droot, provider=provider, model="gpt-6-sol" if provider == "codex" else None, timeout_minutes=0.015
    )
    assert result["state"] != "done" and result["attempt"] == 1
    assert result["stop_reason"] in ("idle", "timeout")
    assert result.get("result", {}).get("summary") != LATEST
    assert not (Path(result["worktree"]) / "worker.txt").exists()
    if rpc:
        assert rpc.turns == 2
        assert result["last_result"]["terminal_reason"] == "timeout"
    else:
        assert fake_claude.calls()[0]["steers"] == [ANSWER]
