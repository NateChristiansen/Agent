"""What a manager's tab shows while something else waits.

A background subagent's permission prompt left its manager's tab on ⚙️ or ⏳. The subagent's own
hook events carry `agent_id`, and the activity hook ignored every one of them,
`PermissionRequest` included. Its session_id is the manager's.

The state half: `Stop` carries `background_tasks`, Claude Code's own list of what is still in
flight. The tracked set could miss a task (an idle manager showed ✅ while a subagent still ran),
so a `Stop` that reports the list now settles the set.

Payload shapes follow the Claude Code hooks reference and real hook captures."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import hooks, sessions, tab_drain

SID = "ae3b0000-0000-4000-8000-000000000001"
AGENT = "a3bf0000000000001"
NAME = "mgr-008-x"
TAB = "mgr-008-x · cl ae3b"


def _setup(tmp_root: Path, monkeypatch, state: str, pending: dict[str, str]) -> list[str]:
    sessions.set_record(tmp_root, SID, pid=4242, name=NAME)
    sessions.set_field(tmp_root, SID, "activity", {"state": state})
    sessions.set_field(tmp_root, SID, "background_tasks", pending)
    labels: list[str] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: labels.append(label) or {"started": True},
    )
    return labels


def _common(event: str, tmp_path: Path) -> dict:
    return {
        "session_id": SID,
        "transcript_path": str(tmp_path / f"{SID}.jsonl"),
        "cwd": "/home/user/workspace/Agent",
        "permission_mode": "default",
        "hook_event_name": event,
    }


def _subagent(event: str, tmp_path: Path, agent: str = AGENT) -> dict:
    payload = {
        **_common(event, tmp_path),
        "agent_id": agent,
        "agent_type": "general-purpose",
        "tool_name": "Bash",
        "tool_input": {"command": "uv run pytest -q", "description": "Run the full test suite"},
    }
    if event == "PermissionRequest":
        payload["permission_suggestions"] = [
            {
                "type": "addRules",
                "rules": [{"toolName": "Bash", "ruleContent": "uv run pytest:*"}],
                "behavior": "allow",
                "destination": "localSettings",
            }
        ]
    else:
        payload["tool_use_id"] = "toolu_01ABC"
        payload["tool_response"] = {"stdout": "1 passed", "stderr": "", "interrupted": False}
    return payload


def _notice(task_id: str) -> str:
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
        "<status>completed</status>\n<summary>Agent finished</summary>\n</task-notification>"
    )


def test_a_subagent_permission_prompt_shows_the_hand_until_it_resolves(tmp_root: Path, monkeypatch, tmp_path):
    labels = _setup(tmp_root, monkeypatch, "idle", {AGENT: "t"})

    hooks.activity(tmp_root, _subagent("PermissionRequest", tmp_path))
    assert labels[-1] == f"✋ {TAB}"
    # The manager itself is still at rest: the hand comes from the subagent, not its activity.
    assert (sessions.get(tmp_root, SID) or {})["activity"]["state"] == "idle"

    # Allowed: the subagent's tool runs, and the tab returns to what it showed before.
    hooks.activity(tmp_root, _subagent("PostToolUse", tmp_path))
    assert labels[-1] == f"⏳ {TAB}"

    # Denied: no PostToolUse follows. The subagent then ends, and its finished notice clears it.
    hooks.activity(tmp_root, _subagent("PermissionRequest", tmp_path))
    assert labels[-1] == f"✋ {TAB}"
    hooks.activity(tmp_root, {**_common("UserPromptSubmit", tmp_path), "prompt": _notice(AGENT)})
    assert labels[-1] == f"⚙️ {TAB}"
    assert not (sessions.get(tmp_root, SID) or {}).get("subagent_permission")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_owner_question_shows_the_hand_through_stop_until_the_owner_answers(
    tmp_root: Path, monkeypatch, tmp_path
):
    """A question asked in plain text ends the turn like any other, and `Stop` showed ✅.
    The manager signals a question explicitly with `owner_question`, which persists through
    `Stop`."""
    from mcp import Client

    from agent_cli.mcp import server as mcp_server

    labels = _setup(tmp_root, monkeypatch, "working", {AGENT: "t"})
    server = mcp_server.build_server(tmp_root, "manager")
    async with Client(server) as client:
        result = await client.call_tool(
            "owner_question", {"session_id": SID, "summary": "Which repo holds the fix?"}
        )
    assert not result.is_error, result
    assert labels[-1] == f"✋ {TAB}"

    tool = {**_common("PostToolUse", tmp_path), "tool_name": "mcp__agent__owner_question"}
    hooks.activity(tmp_root, {**tool, "tool_input": {}, "tool_response": {}})
    stop = {**_common("Stop", tmp_path), "stop_hook_active": False, "last_assistant_message": "Which repo?"}
    hooks.activity(tmp_root, {**stop, "background_tasks": [{"id": AGENT, "type": "subagent"}]})
    assert labels[-1] == f"✋ {TAB}"

    # A background task's notice is not the owner's answer.
    hooks.activity(tmp_root, {**_common("UserPromptSubmit", tmp_path), "prompt": _notice(AGENT)})
    assert labels[-1] == f"✋ {TAB}"
    hooks.activity(tmp_root, {**stop, "background_tasks": []})
    assert labels[-1] == f"✋ {TAB}"

    hooks.activity(tmp_root, {**_common("UserPromptSubmit", tmp_path), "prompt": "the harness repo"})
    from agent_cli.ops import sessions as session_ops

    session_ops.owner_question(tmp_root, session_id=SID, resolution="answered")
    assert labels[-1] == f"⚙️ {TAB}"
    assert not (sessions.get(tmp_root, SID) or {}).get("owner_question_pending")


def test_a_question_over_a_running_subagent_reaches_the_tab_through_a_flaky_obsidian(
    tmp_root: Path, monkeypatch, tmp_path
):
    """Session ae3b, 2026-10-04: `owner_question` while a background subagent ran, and the tab
    kept ⏳ at `Stop`. The precedence was right — `.agent/tabs/` asked for ✋ — but the drainer's
    evals failed three times in a row (Obsidian answered none of the tabs in those batches, and
    the next batch found all of them), and each failure counted against the label's retry cap,
    so ✋ was refused as unpaintable for good. A failed eval says nothing about the tab.

    Then the owner answers while the subagent still runs: ⚙️, and ⏳ at the next `Stop`."""
    import json
    import os
    import subprocess

    from agent_cli.dispatch import opener
    from agent_cli.ops import sessions as session_ops

    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)

    class Process:
        pid = 99999

    monkeypatch.setattr(tab_drain.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(tab_drain.core, "is_alive", lambda pid: bool(pid))
    monkeypatch.setattr(opener, "obsidian_cli", lambda: "/usr/local/bin/obsidian")
    shown: list[str] = []  # what the tab reads
    failures = {"left": 0}

    real_run = subprocess.run

    def obsidian(argv, **kw):
        if argv[:2] != ["/usr/local/bin/obsidian", "eval"]:  # `ps` for the session's name
            return real_run(argv, **kw)
        if failures["left"]:
            failures["left"] -= 1
            return subprocess.CompletedProcess(argv, 1, "", "Obsidian did not answer")
        code = argv[2].removeprefix("code=")
        entries = json.loads(code.split("const entries = ", 1)[1].split("; const leaves", 1)[0])
        shown.extend(entry["label"] for entry in entries)
        keys = ",".join(entry["key"] for entry in entries)
        return subprocess.CompletedProcess(argv, 0, f"AGENT_TAB_RESULTS:{keys}\n", "")

    monkeypatch.setattr(tab_drain.subprocess, "run", obsidian)

    def drain() -> None:
        state = tab_drain._state(tmp_root)
        state["runner"] = os.getpid()
        tab_drain._save_state(tmp_root, state)
        tab_drain.run(tmp_root)

    sessions.set_record(tmp_root, SID, pid=4242, name=NAME)
    sessions.set_field(tmp_root, SID, "activity", {"state": "working"})
    sessions.set_field(tmp_root, SID, "background_tasks", {AGENT: "t"})
    stop = {**_common("Stop", tmp_path), "stop_hook_active": False, "last_assistant_message": "Running."}
    running = [{"id": AGENT, "type": "subagent", "status": "running"}]

    hooks.activity(tmp_root, {**stop, "background_tasks": running})
    drain()
    assert shown[-1] == f"⏳ {TAB}"
    hooks.activity(tmp_root, {**_common("UserPromptSubmit", tmp_path), "prompt": "go on"})
    drain()
    assert shown[-1] == f"⚙️ {TAB}"

    session_ops.owner_question(tmp_root, session_id=SID, summary="Which repo holds the fix?")
    failures["left"] = tab_drain.RETRY_LIMIT
    hooks.activity(tmp_root, {**stop, "background_tasks": running})
    drain()
    assert shown[-1] == f"✋ {TAB}"
    # the paint of the snapshot's label is the drainer's record; the session's names are the hooks'
    painted = tab_drain._state(tmp_root)["painted"][tab_drain.drain_key("manager", SID)]
    assert painted["label"] == f"✋ {TAB}"

    hooks.activity(tmp_root, {**_common("UserPromptSubmit", tmp_path), "prompt": "the harness repo"})
    session_ops.owner_question(tmp_root, session_id=SID, resolution="answered")
    drain()
    assert shown[-1] == f"⚙️ {TAB}"
    hooks.activity(tmp_root, {**stop, "background_tasks": running})
    drain()
    assert shown[-1] == f"⏳ {TAB}"


def test_stop_settles_background_tasks_from_the_list_it_reports(tmp_root: Path, monkeypatch, tmp_path):
    transcript = tmp_path / f"{SID}.jsonl"
    transcript.write_text("", encoding="utf-8")
    labels = _setup(tmp_root, monkeypatch, "working", {})
    running = {
        "id": AGENT,
        "type": "subagent",
        "status": "running",
        "description": "tab waiting states",
        "agent_type": "general-purpose",
    }
    stop = {
        **_common("Stop", tmp_path),
        "stop_hook_active": False,
        "last_assistant_message": "Both are running; I'm ending the turn.",
        "session_crons": [],
    }

    # A subagent the tracked set never saw open: Claude Code still lists it, so the tab waits.
    hooks.activity(tmp_root, {**stop, "background_tasks": [running]})
    assert labels[-1] == f"⏳ {TAB}"
    assert set((sessions.get(tmp_root, SID) or {})["background_tasks"]) == {AGENT}

    # Once a task appeared in a Stop list, later omission settles a missed finished notice.
    sessions.set_field(tmp_root, SID, "activity", {"state": "working"})
    hooks.activity(tmp_root, {**stop, "background_tasks": []})
    assert labels[-1] == f"✅ {TAB}"
    hooks.activity(tmp_root, {**_common("UserPromptSubmit", tmp_path), "prompt": _notice(AGENT)})
    hooks.activity(tmp_root, {**stop, "background_tasks": []})
    assert labels[-1] == f"✅ {TAB}"
    assert (sessions.get(tmp_root, SID) or {})["background_tasks"] == {}
