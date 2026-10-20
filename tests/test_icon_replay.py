"""`scripts/icon_replay.py` takes the expected glyph from
the transcripts alone and the shown glyph from the label code fed the window's hook events.
These cases pin the ground-truth rules and one replay through this tree's own label code."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from agent_cli import core
from agent_cli.vault import live

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "icon_replay.py"
_spec = importlib.util.spec_from_file_location("icon_replay", SCRIPT)
assert _spec is not None and _spec.loader is not None
replay_mod = importlib.util.module_from_spec(_spec)
sys.modules["icon_replay"] = replay_mod
_spec.loader.exec_module(replay_mod)

SID = "aaaa1111-2222-3333-4444-555566667777"


def at(minute: int) -> str:
    return f"2026-10-12T10:{minute:02d}:00+00:00"


def t(minute: int) -> float:
    stamp = replay_mod.ts(at(minute))
    assert stamp is not None
    return stamp


def _user(minute: int, text: str, **extra) -> dict:
    return {"type": "user", "timestamp": at(minute), "message": {"content": text}, **extra}


def _assistant(minute: int, *tools: tuple[str, str]) -> dict:
    blocks = [{"type": "tool_use", "id": tid, "name": name, "input": {}} for name, tid in tools]
    return {
        "type": "assistant",
        "timestamp": at(minute),
        "message": {"content": blocks or [{"type": "text"}]},
    }


def _result(minute: int, tid: str) -> dict:
    content = [{"type": "tool_result", "tool_use_id": tid}]
    return {"type": "user", "timestamp": at(minute), "message": {"content": content}}


def _end(minute: int) -> dict:
    return {"type": "system", "subtype": "turn_duration", "timestamp": at(minute)}


def test_claude_turns_start_when_the_model_answers_and_questions_wait_for_their_result():
    records = [
        _user(0, "This session is being continued", isCompactSummary=True),  # runs no turn
        _user(5, "go", origin={"kind": "human"}),
        _assistant(6, ("AskUserQuestion", "q1")),
        _result(9, "q1"),
        _assistant(10, ("mcp__agent__owner_question", "o1")),
        _end(11),
        _user(20, "answer", origin={"kind": "human"}),
        _assistant(21),
        _end(22),
    ]
    truth = replay_mod.claude_truth(records)
    assert truth.turns == [(t(5), t(11)), (t(20), t(22))]
    assert sorted(truth.hands) == [(t(6), t(9)), (t(10), t(20))]


def test_codex_turns_and_questions():
    def ev(minute: int, payload: dict) -> dict:
        return {"timestamp": at(minute), "type": "event_msg", "payload": payload}

    records = [
        ev(0, {"type": "task_started", "turn_attribution": {"turn_trigger": "user"}}),
        ev(1, {"type": "function_call", "name": "request_user_input", "call_id": "c1"}),
        ev(3, {"type": "function_call_output", "call_id": "c1"}),
        ev(4, {"type": "custom_tool_call", "input": "await tools.mcp__agent__owner_question({})"}),
        ev(5, {"type": "task_complete"}),
        ev(8, {"type": "task_started", "turn_attribution": {"turn_trigger": "queue"}}),  # a wake
        ev(9, {"type": "task_complete"}),
        ev(12, {"type": "task_started", "turn_attribution": {"turn_trigger": "user"}}),
        ev(13, {"type": "task_complete"}),
    ]
    truth = replay_mod.codex_truth(records)
    assert truth.turns == [(t(0), t(5)), (t(8), t(9)), (t(12), t(13))]
    assert sorted(truth.hands) == [(t(1), t(3)), (t(4), t(12))]


@pytest.mark.parametrize("supervisor_pid", [None, 12345])
def test_a_finished_unhandled_worker_is_not_shown_as_waiting(monkeypatch, supervisor_pid):
    """Through this tree's label code: once the worker ends, an idle manager reads ✅."""
    monkeypatch.delenv("AGENT_ROOT", raising=False)
    monkeypatch.setattr(core, "is_alive", lambda pid: False)
    running_dispatch = live.running_dispatch

    def row(minute: int, kind: str, **fields) -> dict:
        return {"ts": at(minute), "kind": kind, **fields}

    window = replay_mod.Window(
        start=t(0),
        end=t(30),
        telemetry=[
            row(1, "activity.transition", session=SID, data={"cause": "owner_prompt"}),
            row(2, "dispatch.launch", dispatch="001-mac-01"),
            row(3, "activity.transition", session=SID, data={"cause": "Stop"}),
            row(10, "dispatch.terminal", dispatch="001-mac-01"),
        ],
        sessions={SID: {"session_id": SID, "name": "mgr-030-x", "provider": "claude"}},
        dispatches={
            "001-mac-01": {
                "id": "001-mac-01",
                "feature": 30,
                "state": "done",
                "launched_by_session": SID,
                "supervisor_pid": supervisor_pid,
            }
        },
        transcripts={
            SID: (
                "claude",
                [
                    _user(1, "go", origin={"kind": "human"}),
                    _assistant(1),
                    _end(3),
                    {"type": "system", "subtype": "away_summary", "timestamp": at(30)},  # no turn
                ],
            )
        },
    )
    out = replay_mod.replay(window, {})
    assert live.running_dispatch is running_dispatch
    total = out["total"]
    assert total["seconds"] == 29 * 60
    assert not any("finished worker" in kind for kind in total["kinds"])
    assert total["wrong_seconds"] == 0, total["kinds"]  # ⚙️ 1-3, ⏳ 3-10, ✅ after
