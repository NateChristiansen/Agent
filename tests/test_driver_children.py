"""Audit 10 A5 / plan-r1 R1: parent results stay provisional until delegation finishes.

Replay CX-8's lifecycle stream and cross the watchdog boundary on a fake clock.
The driver outcome and persistence are checked before releasing the child, not just afterward.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import TaskNotificationMessage, TaskStartedMessage, TaskUpdatedMessage

from agent_cli.dispatch import driver, normalize, notice_producers, supervisor, usage
from agent_cli.vault import live

_yield_once = asyncio.sleep


def _started(task_id: str = "child", task_type: str | None = "local_agent") -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        task_type=task_type,
        description="test child",
        uuid="start",
        session_id="s",
    )


def _completed(task_id: str = "child", status: Any = "completed") -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="unused",
        summary="finished",
        uuid="finish",
        session_id="s",
    )


def _result(status: str = "done") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        structured_output={"status": status},
        result="parent result",
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.actions: dict[float, Callable[[], None]] = {}

    async def sleep(self, _seconds: float) -> None:
        await _yield_once(0)
        self.now += 1
        if action := self.actions.pop(self.now, None):
            action()


class Client:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages
        self.delivered: list[object] = []
        self.release: asyncio.Event | None = None
        self.trailing: list[object] = []
        self.hang = False
        self.interrupts = 0
        self.disconnects = 0
        self.on_interrupt: Callable[[], Any] | None = None

    async def connect(self) -> None:
        pass

    async def query(self, _message: str) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnects += 1

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self.on_interrupt:
            await self.on_interrupt()

    async def receive_messages(self) -> AsyncIterator[object]:
        for message in self.messages:
            self.delivered.append(message)
            yield message
        if self.release is not None:
            await self.release.wait()
        for message in self.trailing:
            self.delivered.append(message)
            yield message
        if self.hang:
            await asyncio.Event().wait()


class Harness:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.clock = Clock()
        self.persisted: list[tuple[float, dict[str, Any]]] = []
        self.client = Client([])
        monkeypatch.setattr(driver, "time", SimpleNamespace(monotonic=lambda: self.clock.now))
        monkeypatch.setattr(driver.asyncio, "sleep", self.clock.sleep)
        monkeypatch.setattr(driver, "ClaudeSDKClient", lambda **_: self.client)
        monkeypatch.setattr(driver, "file", lambda root, dispatch_id, suffix: root / (dispatch_id + suffix))
        monkeypatch.setattr(driver, "cancel_requested", lambda *_: False)
        monkeypatch.setattr(supervisor, "transport_factory", lambda _: object())
        monkeypatch.setattr(supervisor, "remember_worker_pid", lambda *_: None)
        monkeypatch.setattr(
            supervisor,
            "persist_result",
            lambda _root, _id, result: self.persisted.append((self.clock.now, result)),
        )
        monkeypatch.setattr(usage, "count", lambda *_, **__: None)
        monkeypatch.setattr(usage, "count_message", lambda *_: None)
        monkeypatch.setattr(live, "HEARTBEAT_SECONDS", 10000)

    def run(self, messages: list[object], *, timeout: float = 120) -> dict[str, Any]:
        self.client.messages = messages
        return asyncio.run(
            asyncio.wait_for(
                driver._drive_worker(
                    self.root,
                    {"id": "probe", "session_id": "s"},
                    ClaudeAgentOptions(),
                    first_message="test",
                    inbox=self.root / "inbox",
                    timeout=timeout,
                    idle_seconds=600,
                ),
                timeout=5,
            )
        )

    def assert_provisional(self) -> None:
        assert self.persisted == []
        assert self.client.disconnects == self.client.interrupts == 0


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


@pytest.mark.parametrize("completion_first", [False, True])
def test_cx8_consumes_child_completion_before_accepting(harness: Harness, completion_first: bool):
    started, result, completed = _started(), _result(), _completed()
    messages = [started, completed, result] if completion_first else [started, result, completed]
    harness.client.release = asyncio.Event()
    harness.client.trailing = [result] if completion_first else [completed]
    harness.clock.actions[1] = harness.assert_provisional
    harness.clock.actions[2] = harness.client.release.set
    outcome = harness.run(messages[:2])
    assert harness.client.delivered == messages
    assert len(harness.persisted) == 1
    assert outcome["exit_code"] == 0 and outcome["stop_reason"] == "result"
    assert outcome["last_result"] == normalize.result_envelope(result)
    rows = [json.loads(line) for line in (harness.root / "probe.out").read_text().splitlines()]
    assert any(row.get("task_id") == "child" and row.get("status") == "completed" for row in rows)


@pytest.mark.parametrize(
    "task_type", ["local_agent", "remote_agent", "in_process_teammate", "local_workflow"]
)
def test_r1_delayed_child_crosses_watchdog_without_acceptance(harness: Harness, task_type: str):
    harness.client.release = asyncio.Event()
    harness.client.trailing = [_completed()]
    harness.clock.actions[31] = harness.assert_provisional
    harness.clock.actions[35] = harness.client.release.set
    outcome = harness.run([_started(task_type=task_type), _result()])
    assert harness.persisted[0][0] >= 35
    assert len(harness.persisted) == 1
    assert outcome["exit_code"] == 0 and outcome["stop_reason"] == "result"
    assert outcome["timed_out"] is False and harness.client.interrupts == 0


@pytest.mark.parametrize("completion_during_interrupt", [False, True])
def test_pending_child_at_attempt_timeout_is_never_accepted(
    harness: Harness, completion_during_interrupt: bool
):
    harness.client.release = asyncio.Event()
    harness.client.trailing = [_completed(), _result()]
    harness.clock.actions[31] = harness.assert_provisional
    if completion_during_interrupt:
        release = harness.client.release

        async def release_during_interrupt() -> None:
            release.set()
            await _yield_once(0)

        harness.client.on_interrupt = release_during_interrupt
    outcome = harness.run([_started(), _result()], timeout=40)
    assert outcome["timed_out"] is True and outcome["stop_reason"] == "timeout"
    assert outcome["exit_code"] == 1 and outcome["last_result"] is None
    assert harness.persisted == [] and harness.client.interrupts == 1


@pytest.mark.parametrize("agent_type", ["architect", "custodian"])
def test_foreground_named_agent_stream_is_unchanged(harness: Harness, agent_type: str):
    messages = [
        AssistantMessage(
            content=[ToolUseBlock(id="tool", name="Agent", input={"subagent_type": agent_type})], model=""
        ),
        _started(),
        _completed(),
        UserMessage(content=[ToolResultBlock(tool_use_id="tool", content="finished")]),
        _result(),
    ]
    outcome = harness.run(messages)
    assert harness.client.delivered == messages and len(harness.persisted) == 1
    assert outcome["exit_code"] == 0 and outcome["stop_reason"] == "result"


def test_no_children_accepts_result_immediately(harness: Harness):
    result = _result()
    outcome = harness.run([result, SystemMessage(subtype="unused", data={})])
    assert harness.client.delivered == [result]
    assert harness.persisted == [(0, normalize.result_envelope(result))]
    assert outcome["exit_code"] == 0 and outcome["stop_reason"] == "result"


@pytest.mark.parametrize("task_type", ["local_bash", "monitor_mcp", "mcp_task", "dream", "unknown", None])
def test_non_agent_task_does_not_hold_result(harness: Harness, task_type: str | None):
    started, result = _started(task_type=task_type), _result()
    harness.client.release = asyncio.Event()
    outcome = harness.run([started, result], timeout=40)
    assert harness.client.delivered == [started, result]
    assert harness.persisted == [(0, normalize.result_envelope(result))]
    assert outcome["exit_code"] == 0 and outcome["stop_reason"] == "result"
    assert outcome["timed_out"] is False and harness.client.interrupts == 0


def test_question_notice_waits_for_pending_children(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    holds: list[str] = []
    state: dict[str, Any] = {"last_result": None, "stop_reason": None}
    question, result = _result("question"), _result()
    monkeypatch.setattr(
        notice_producers, "question_hold", lambda _root, dispatch_id: holds.append(dispatch_id)
    )

    async def messages() -> AsyncIterator[object]:
        yield _started()
        yield question
        assert holds == [] and state.get("question_deadline") is None
        assert state["last_result"] is None and harness.persisted == []
        yield _completed()
        assert holds == ["probe"] and state["question_deadline"] == 600
        assert harness.persisted == [(0, normalize.result_envelope(question))]
        yield result

    monkeypatch.setattr(harness.client, "receive_messages", messages)
    stop = asyncio.Event()
    asyncio.run(
        driver.read_worker(
            harness.root,
            {"id": "probe"},
            cast(Any, harness.client),
            state,
            stop,
            io.StringIO(),
            io.StringIO(),
            600,
        )
    )
    assert stop.is_set() and state["stop_reason"] == "result"
    assert state["last_result"] == normalize.result_envelope(result)
    assert holds == ["probe"]


@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "killed"])
def test_every_terminal_child_status_releases_result(harness: Harness, status: str):
    completion = (
        TaskUpdatedMessage(subtype="task_updated", data={}, task_id="child", patch={"status": status})
        if status == "killed"
        else _completed(status=status)
    )
    outcome = harness.run([_started(), _result(), completion])
    assert outcome["exit_code"] == 0 and len(harness.persisted) == 1


def test_multiple_children_and_unrelated_notifications(harness: Harness):
    harness.client.release = asyncio.Event()
    harness.client.trailing = [_completed("second")]
    harness.clock.actions[31] = harness.assert_provisional
    harness.clock.actions[35] = harness.client.release.set
    outcome = harness.run(
        [
            _started(),
            _started(),  # A duplicate start is still one task.
            _started("second"),
            _result(),
            _completed("unrelated"),
            _completed(),
            TaskUpdatedMessage(
                subtype="task_updated", data={}, task_id="second", patch={"status": "running"}
            ),
        ]
    )
    assert harness.persisted[0][0] >= 35 and outcome["exit_code"] == 0


def test_stream_end_with_pending_child_cannot_accept_parent_result(harness: Harness):
    outcome = harness.run([_started(), _result()])
    assert outcome["stop_reason"] == "stream-end" and outcome["exit_code"] == 1
    assert outcome["last_result"] is None and harness.persisted == []


def test_latest_parent_result_is_accepted_after_child_finishes(harness: Harness):
    result = _result("blocked")
    outcome = harness.run([_started(), _result(), result, _completed()])
    assert outcome["last_result"] == normalize.result_envelope(result)
    assert len(harness.persisted) == 1


def test_plain_system_lifecycle_fields_are_preserved():
    record = normalize.out_record(
        SystemMessage(
            subtype="task_updated",
            data={"task_id": "child", "patch": {"status": "completed", "result": "not copied"}},
        )
    )
    assert record == {"type": "system", "subtype": "task_updated", "task_id": "child", "status": "completed"}
    assert normalize.out_record(SystemMessage(subtype="init", data={"other": "unchanged"})) == {
        "type": "system",
        "subtype": "init",
    }


@pytest.mark.parametrize("plain_system", [False, True])
def test_task_type_is_preserved_without_task_metadata(plain_system: bool):
    message = (
        SystemMessage(
            subtype="task_started",
            data={"task_id": "child", "task_type": "local_agent", "description": "not copied"},
        )
        if plain_system
        else _started()
    )
    assert normalize.out_record(message) == {
        "type": "system",
        "subtype": "task_started",
        "task_id": "child",
        "task_type": "local_agent",
    }
