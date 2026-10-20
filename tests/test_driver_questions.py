"""A question hold follows the worker, not the arrival order of messages.

The manager's answer was delivered, then the worker's question result arrived
seconds later and armed a 600-second hold, and the worker went on to commit its work while the
supervisor waited, then finalized the stale question. A result ends a turn, so assistant activity
after a question means an answer is being worked on: the hold clears."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from agent_cli.dispatch import driver, notice_producers, supervisor, usage
from agent_cli.dispatch import state as state_mod
from agent_cli.vault import live


class Result:
    pass


class Assistant:
    usage = None
    model = None


class System:
    pass


def _state() -> dict[str, Any]:
    return {"last_result": None, "stop_reason": None, "first_result_at": None}


Wired = tuple[list[str], list[str], list[str]]


@pytest.fixture
def wired() -> Iterator[Wired]:
    """`(statuses to return, question holds noticed, answers noticed)`."""
    answered: list[str] = []
    holds: list[str] = []
    statuses: list[str] = []

    def status(_envelope: object) -> str:
        return statuses.pop(0) if statuses else "done"

    def hold(_root: Path, dispatch_id: str) -> None:
        holds.append(dispatch_id)

    def answer(_root: Path, dispatch_id: str) -> None:
        answered.append(dispatch_id)

    with (
        patch.object(state_mod, "POLL_SECONDS", 0.001),
        patch.object(driver, "cancel_requested", return_value=False),
        patch.object(live, "write_quietly"),
        patch.object(supervisor, "persist_result"),
        patch.object(driver, "ResultMessage", Result),
        patch.object(driver, "AssistantMessage", Assistant),
        patch.object(driver, "SystemMessage", System),
        patch.object(driver, "out_record", return_value={}),
        patch.object(driver, "result_envelope", return_value={"structured_output": {}}),
        patch.object(driver, "return_status", side_effect=status),
        patch.object(notice_producers, "question_hold", side_effect=hold),
        patch.object(notice_producers, "answered", side_effect=answer),
        patch.object(usage, "count"),
    ):
        yield statuses, holds, answered


def _read(messages: list[object], state: dict[str, Any]) -> None:
    class Client:
        async def receive_messages(self) -> AsyncIterator[object]:
            for m in messages:
                yield m

    asyncio.run(
        driver.read_worker(
            Path("/unused"),
            {"id": "000-probe"},
            cast(Any, Client()),
            state,
            asyncio.Event(),
            io.StringIO(),
            io.StringIO(),
            600,
        )
    )


def test_an_answer_delivered_before_the_question_result_clears_the_hold(wired: Wired):
    """The live ordering: steer first, then the question result, then the worker keeps working."""
    statuses, holds, answered = wired
    statuses.extend(["question", "done"])
    state = _state()
    stop = asyncio.Event()
    sent: list[str] = []

    class PollClient:
        async def query(self, message: str) -> None:
            sent.append(message)
            stop.set()

    with patch.object(driver, "take_inbox", return_value="use a checkout-local output path"):
        asyncio.run(
            driver.poll_worker(
                cast(Any, PollClient()),
                Path("/unused"),
                {"id": "000-probe"},
                state,
                stop,
                Path("/unused"),
                io.StringIO(),
                60,
            )
        )
    assert sent and state.get("question_deadline") is None and state["answer_pending"] is True
    _read([Result(), Assistant(), Result()], state)
    assert holds == [] and answered == [], "the question never armed a hold: its answer was already sent"
    assert state.get("question_deadline") is None and state.get("answer_wait_until") is None
    assert state["stop_reason"] == "result"


def _steer_first(state: dict[str, Any]) -> None:
    stop = asyncio.Event()

    class PollClient:
        async def query(self, message: str) -> None:
            stop.set()

    with patch.object(driver, "take_inbox", return_value="the answer"):
        asyncio.run(
            driver.poll_worker(
                cast(Any, PollClient()),
                Path("/unused"),
                {"id": "000-probe"},
                state,
                stop,
                Path("/unused"),
                io.StringIO(),
                60,
            )
        )


def test_an_early_answer_with_no_activity_never_rearms_the_question(wired: Wired):
    """Codex's probe: the answer arrives, then the question result, then nothing. No question hold
    is armed; the attempt ends idle only once the window for acting on the answer passes."""
    statuses, holds, _ = wired
    statuses.append("question")
    state = _state()
    _steer_first(state)
    _read([Result()], state)
    assert holds == [] and state.get("question_deadline") is None
    assert state.get("answer_wait_until") is not None
    state["answer_wait_until"] = 0.0  # the window has passed
    stop = asyncio.Event()
    with patch.object(driver, "take_inbox", return_value=None):
        asyncio.run(
            driver.poll_worker(
                cast(Any, object()),
                Path("/unused"),
                {"id": "000-probe"},
                state,
                stop,
                Path("/unused"),
                io.StringIO(),
                60,
            )
        )
    assert state["stop_reason"] == "idle"


def test_a_steer_consumed_by_a_finished_turn_does_not_carry_over(wired: Wired):
    statuses, holds, _ = wired
    statuses.extend(["done"])
    state = _state()
    _steer_first(state)
    _read([Result()], state)
    assert state["answer_pending"] is False and holds == []


def test_a_genuine_question_with_no_activity_keeps_its_hold(wired: Wired):
    statuses, holds, answered = wired
    statuses.append("question")
    state = _state()
    _read([Result()], state)
    assert holds == ["000-probe"] and answered == []
    assert state.get("question_deadline") is not None


def test_a_second_question_after_the_answer_arms_a_new_hold(wired: Wired):
    statuses, holds, answered = wired
    statuses.extend(["question", "question"])
    state = _state()
    _read([Result(), Assistant(), Result()], state)
    assert holds == ["000-probe", "000-probe"] and answered == ["000-probe"]
    assert state.get("question_deadline") is not None


def test_an_answer_after_the_question_still_clears_through_the_steer(wired: Wired):
    statuses, _holds, answered = wired
    statuses.append("question")
    state = _state()
    _read([Result()], state)
    stop = asyncio.Event()

    class PollClient:
        async def query(self, message: str) -> None:
            stop.set()

    with patch.object(driver, "take_inbox", return_value="the answer"):
        asyncio.run(
            driver.poll_worker(
                cast(Any, PollClient()),
                Path("/unused"),
                {"id": "000-probe"},
                state,
                stop,
                Path("/unused"),
                io.StringIO(),
                60,
            )
        )
    assert state.get("question_deadline") is None and answered == ["000-probe"]


def test_a_stream_that_ends_stops_the_attempt(wired: Wired):
    """Process death: the stream ends without a return."""
    state = _state()
    _read([Assistant()], state)
    assert state["stop_reason"] == "stream-end"
