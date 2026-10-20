from __future__ import annotations

import importlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_cli import sessions
from agent_cli.ops import sessions as session_ops
from agent_cli.telemetry import recorder as recorder_mod
from agent_cli.telemetry.recorder import main_of
from agent_cli.telemetry.sink import Sink

# the package re-exports a function named `activity`, which hides the submodule attribute
activity_mod = importlib.import_module("agent_cli.hooks.activity")
SID = "s-owner"
SUMMARY = "Which repo holds the billing fix?"


@pytest.fixture(autouse=True)
def quiet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loss = tmp_path / "losstmp"
    loss.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(loss))
    monkeypatch.setattr(recorder_mod, "_process", None)
    monkeypatch.setattr(activity_mod, "refresh_tab", lambda *a, **k: None)


def questions(root: Path) -> list[dict[str, Any]]:
    events = Sink.for_main(main_of(root)).events
    lines = [line for p in sorted(events.glob("*.jsonl")) for line in p.read_text().splitlines()]
    return [e for e in map(json.loads, lines) if e["kind"] == "workflow.owner_question"]


def ask(root: Path) -> str:
    return session_ops.owner_question(root, session_id=SID, summary=SUMMARY).data["question_id"]


def answer(root: Path) -> None:
    activity_mod.activity(
        root, {"session_id": SID, "hook_event_name": "UserPromptSubmit", "prompt": "the api repo"}
    )
    session_ops.owner_question(root, session_id=SID, resolution="answered")


def test_one_question_asked_twice_keeps_one_id_and_one_event(tmp_root):
    sessions.set_record(tmp_root, SID, feature={"id": 22, "slug": "faster-test-runs", "dir": "features/x"})
    first = ask(tmp_root)
    assert ask(tmp_root) == first
    events = questions(tmp_root)
    assert [e["event_id"] for e in events] == [first]
    assert (events[0]["session"], events[0]["feature"]) == (SID, "022")
    assert events[0]["data"] == {"question_id": first, "route": "manual"}


def test_the_owner_answer_retires_the_id_and_the_next_question_gets_a_new_one(tmp_root):
    sessions.set_record(tmp_root, SID)
    first = ask(tmp_root)
    answer(tmp_root)
    record = sessions.get(tmp_root, SID) or {}
    assert not record.get("owner_question_pending") and record.get("owner_question_id") is None
    second = ask(tmp_root)
    assert second != first
    assert [e["event_id"] for e in questions(tmp_root)] == [first, second]


def test_the_event_holds_no_summary_or_prompt_text(tmp_root):
    sessions.set_record(tmp_root, SID)
    ask(tmp_root)
    answer(tmp_root)
    text = "".join(p.read_text() for p in Sink.for_main(main_of(tmp_root)).events.glob("*.jsonl"))
    assert "billing" not in text and "api repo" not in text
    assert set(questions(tmp_root)[0]["data"]) == {"question_id", "route"}


@pytest.mark.parametrize("resolution", ["answered", "withdrawn", "replaced"])
def test_manual_resolution_is_recorded_once_in_telemetry(tmp_root, resolution):
    sessions.set_record(tmp_root, SID)
    first = ask(tmp_root)
    session_ops.owner_question(tmp_root, session_id=SID, resolution=resolution)
    session_ops.owner_question(tmp_root, session_id=SID, resolution=resolution)
    events = cleared(tmp_root)
    assert len(events) == 1
    assert events[0]["data"]["question_id"] == first
    assert events[0]["data"]["reason"] == resolution


def test_replacing_a_manual_summary_records_the_old_question_as_replaced(tmp_root):
    sessions.set_record(tmp_root, SID)
    first = ask(tmp_root)
    second = session_ops.owner_question(tmp_root, session_id=SID, summary="Should I merge?").data[
        "question_id"
    ]
    assert second != first
    assert [(e["data"]["question_id"], e["data"]["reason"]) for e in cleared(tmp_root)] == [
        (first, "replaced")
    ]


def test_a_lost_write_still_shows_the_question_and_the_next_call_retries_its_id(tmp_root, monkeypatch):
    sessions.set_record(tmp_root, SID)
    original = recorder_mod.append

    def broken(path, line):
        raise OSError("disk gone")

    monkeypatch.setattr(recorder_mod, "append", broken)
    first = ask(tmp_root)
    assert (sessions.get(tmp_root, SID) or {}).get("owner_question_pending") is True
    assert questions(tmp_root) == []
    monkeypatch.setattr(recorder_mod, "append", original)
    monkeypatch.setattr(recorder_mod, "_process", None)
    assert ask(tmp_root) == first
    assert [e["event_id"] for e in questions(tmp_root)] == [first]


# ---------------------------------------------------------------- one lifecycle (D3)


def cleared(root: Path) -> list[dict[str, Any]]:
    events = Sink.for_main(main_of(root)).events
    lines = [line for p in sorted(events.glob("*.jsonl")) for line in p.read_text().splitlines()]
    return [e for e in map(json.loads, lines) if e["kind"] == "workflow.owner_question_cleared"]


def hook(root: Path, event: str, *, tool: str = "", prompt: str | None = None, codex: bool = False) -> None:
    payload: dict[str, Any] = {"session_id": SID, "hook_event_name": event}
    if tool:
        payload["tool_name"] = tool
    if prompt is not None:
        payload["prompt"] = prompt
    (activity_mod.codex_activity if codex else activity_mod.activity)(root, payload)


def test_a_repeated_manual_question_keeps_its_id_and_its_first_time(tmp_root, monkeypatch):
    sessions.set_record(tmp_root, SID)
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:00:00-06:00")
    first = ask(tmp_root)
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:05:00-06:00")
    assert ask(tmp_root) == first
    record = sessions.get(tmp_root, SID) or {}
    assert record["owner_question_at"].startswith("2026-10-08T10:00:00")
    assert record["owner_question_route"] == "manual"


def test_machine_wakes_never_clear_and_the_owner_answer_clears_once(tmp_root, monkeypatch):
    from agent_cli.ops.peer_wake import DISPATCH_WAKE_TEXT, WAKE_TEXT

    sessions.set_record(tmp_root, SID)
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:00:00-06:00")
    first = ask(tmp_root)
    for wake in (WAKE_TEXT, DISPATCH_WAKE_TEXT):
        hook(tmp_root, "UserPromptSubmit", prompt=wake)
    assert (sessions.get(tmp_root, SID) or {}).get("owner_question_pending") is True
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:07:30-06:00")
    answer(tmp_root)
    answer(tmp_root)
    events = cleared(tmp_root)
    assert len(events) == 1
    assert events[0]["data"] == {"question_id": first, "route": "manual", "reason": "answered", "open_s": 450}


def test_manual_reply_records_answer_only_once_at_stop(tmp_root, monkeypatch):
    sessions.set_record(tmp_root, SID)
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:00:00-06:00")
    first = ask(tmp_root)
    monkeypatch.setenv("AGENT_NOW", "2026-10-08T10:07:30-06:00")
    hook(tmp_root, "UserPromptSubmit", prompt="the api repo")
    assert cleared(tmp_root) == []
    assert (sessions.get(tmp_root, SID) or {})["owner_question_id"] == first
    hook(tmp_root, "PostToolUse", tool="exec")
    hook(tmp_root, "Stop")
    hook(tmp_root, "Stop")
    session_ops.owner_question(tmp_root, session_id=SID, resolution="answered")
    assert [e["data"] for e in cleared(tmp_root)] == [
        {"question_id": first, "route": "manual", "reason": "answered", "open_s": 450}
    ]


def test_a_sync_question_is_raised_by_the_prompt_and_cleared_by_its_answer(tmp_root):
    sessions.set_record(tmp_root, SID)
    hook(tmp_root, "PreToolUse", tool="AskUserQuestion")
    record = sessions.get(tmp_root, SID) or {}
    assert record["owner_question_pending"] is True and record["owner_question_route"] == "sync"
    raised = questions(tmp_root)
    assert [e["data"]["route"] for e in raised] == ["sync"]
    hook(tmp_root, "PostToolUse", tool="AskUserQuestion")  # the owner answered in the prompt
    assert not (sessions.get(tmp_root, SID) or {}).get("owner_question_pending")
    assert [e["data"]["route"] for e in cleared(tmp_root)] == ["sync"]


def test_an_async_codex_question_stays_open_until_the_owner_prompts(tmp_root, monkeypatch):
    monkeypatch.setattr(activity_mod.sessions, "resolve_identity", lambda: (None, False))
    sessions.set_record(tmp_root, SID)
    hook(tmp_root, "PostToolUse", tool="request_user_input_async", codex=True)
    record = sessions.get(tmp_root, SID) or {}
    assert record["owner_question_pending"] is True and record["owner_question_route"] == "async"
    hook(tmp_root, "PostToolUse", tool="exec", codex=True)
    hook(tmp_root, "Stop", codex=True)
    assert (sessions.get(tmp_root, SID) or {}).get("owner_question_pending") is True
    hook(tmp_root, "UserPromptSubmit", prompt="yes, both", codex=True)
    assert not (sessions.get(tmp_root, SID) or {}).get("owner_question_pending")
    assert [e["data"]["route"] for e in cleared(tmp_root)] == ["async"]


def _codex_manager(root: Path, last_event: str | None) -> None:
    sessions.set_record(root, SID, name="mgr-codex")
    activity = {"state": "idle", "at": last_event} if last_event else None
    sessions.update_record(
        root,
        SID,
        lambda r: {**r, "pid": None, "pid_unknown": True, "activity": activity},
    )


def _asked_of(root: Path) -> list[dict[str, Any]]:
    from agent_cli.vault import live

    return [i for i in live.obligations(root)["needs_you"] if i.get("session_id") == SID]


def test_a_fresh_codex_session_with_no_activity_yet_shows_its_question(tmp_root):
    _codex_manager(tmp_root, None)
    ask(tmp_root)
    assert [i["text"] for i in _asked_of(tmp_root)] == [SUMMARY]


def test_a_year_old_pending_question_drops_out_of_needs_you(tmp_root, monkeypatch):
    from datetime import timedelta

    from agent_cli import core

    _codex_manager(tmp_root, None)
    ask(tmp_root)
    monkeypatch.setenv("AGENT_NOW", core.iso(core.now() + timedelta(days=365)))
    assert _asked_of(tmp_root) == []


@pytest.mark.parametrize("age_min", [1, 90])
def test_a_codex_manager_question_reaches_needs_you_with_its_text(tmp_root, age_min):
    from datetime import timedelta

    from agent_cli import core
    from agent_cli.vault import live

    _codex_manager(tmp_root, core.iso(core.now() - timedelta(minutes=age_min)))
    ask(tmp_root)
    items = [i for i in live.obligations(tmp_root)["needs_you"] if i.get("session_id") == SID]
    assert [(i["reason"], i["text"]) for i in items] == [("question", SUMMARY)]
    answer(tmp_root)
    assert not [i for i in live.obligations(tmp_root)["needs_you"] if i.get("session_id") == SID]


# Plan §E2 retires legacy Dataview timers; the plugin owns snapshot and tick updates.
@pytest.mark.skipif(shutil.which("node") is None, reason="Node runs LivePane")
def test_the_needs_you_view_draws_a_question_s_text(tmp_root, tmp_path):
    from agent_cli.vault import live
    from live_pane_helpers import texts, view

    _codex_manager(tmp_root, None)
    ask(tmp_root)
    snap = json.loads(json.dumps(live.build(tmp_root), default=str))
    out = texts(view(tmp_path, "needs_you", 0, snap))
    assert "mgr-codex" in out and "has a question for you" in out
    assert SUMMARY in out


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runs LivePane")
def test_an_earlier_snapshot_cannot_repaint_after_the_latest_update(tmp_root, tmp_path):
    from agent_cli.vault import live
    from live_pane_helpers import texts, view

    _codex_manager(tmp_root, None)
    ask(tmp_root)
    snap = json.loads(json.dumps(live.build(tmp_root), default=str))
    old = {**snap, "needs_you": []}
    assert SUMMARY in texts(view(tmp_path, "needs_you", 1, snap, prior=old))


def test_the_owner_question_contract_says_the_question_must_be_on_screen(tmp_root):
    sessions.set_record(tmp_root, SID)
    op = session_ops.owner_question(tmp_root, session_id=SID, summary=SUMMARY)
    assert "visible" in op.text
    from agent_cli.mcp import specs_repos

    spec = specs_repos.REPO_TOOLS["owner_question"]
    assert "AskUserQuestion" in spec.description and "visible" in spec.description
