"""Offline checks of the native MCP grant fixture and its evidence rules.

None of these launches a worker or reaches a model, so none of them is native enforcement
evidence: they show the fixture, detector, control, composition and verdict rules behave as the
live cells (`native_mcp_grants.py --run-live`) rely on.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import native_mcp_grants as n3
from fixtures.mcp_grants_server import ALLOWED_TOOL, CALLS_FILE, NONCE_FILE, SENTINEL_TOOL, SERVER_NAME

NONCE = "n3-0123456789abcdef01234567"


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "live-state"
    state.mkdir()
    (state / NONCE_FILE).write_text(NONCE + "\n", encoding="utf-8")
    return state


WORKER_PID = 4242
SESSION = "00000000-0000-4000-8000-000000000044"


def _transcript(
    directory: Path,
    *,
    session: str = SESSION,
    use_id: str = "toolu_01",
    result_id: str = "toolu_01",
    **over: Any,
) -> str:
    """A Claude session JSONL in its native shape: an assistant tool_use, then a user tool_result."""
    use = {"type": "tool_use", "id": use_id, "name": over.get("name", n3.GRANT), "input": {}}
    result = {
        "type": "tool_result",
        "tool_use_id": result_id,
        "content": [{"type": "text", "text": f"n3 fixture nonce: {over.get('nonce', NONCE)}"}],
        "is_error": over.get("is_error", False),
    }
    lines = [
        {"type": "assistant", "sessionId": session, "message": {"role": "assistant", "content": [use]}},
        {
            "type": "user",
            "sessionId": over.get("result_session", session),
            "message": {"role": "user", "content": [result]},
        },
    ]
    path = directory / over.get("filename", f"{SESSION}.jsonl")
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return str(path)


def _lines(directory: Path, *entries: dict[str, Any]) -> str:
    path = directory / f"{SESSION}.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in entries), encoding="utf-8")
    return str(path)


def _use(use_id: str = "toolu_01", name: str = n3.GRANT, *, kind: str = "assistant") -> dict[str, Any]:
    block = {"type": "tool_use", "id": use_id, "name": name, "input": {}}
    return {"type": kind, "sessionId": SESSION, "message": {"role": kind, "content": [block]}}


def _result(use_id: str = "toolu_01", content: Any = None, *, kind: str = "user") -> dict[str, Any]:
    text = [{"type": "text", "text": f"n3 fixture nonce: {NONCE}"}] if content is None else content
    block = {"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": False}
    return {"type": kind, "sessionId": SESSION, "message": {"role": kind, "content": [block]}}


def _block(entry: dict[str, Any], **over: Any) -> dict[str, Any]:
    """`entry` with its single tool block's fields replaced."""
    copy = json.loads(json.dumps(entry))
    copy["message"]["content"][0].update(over)
    return copy


def _record(*events: dict[str, Any], denials: list[Any] | None = None, **over: Any) -> dict[str, Any]:
    return {
        "id": "002-mac-01",
        "attempt": 1,
        "max_attempts": 1,
        "auto_resumes": [],
        "claude_pid": WORKER_PID,
        "provider": "claude",
        "session_id": SESSION,
        "result": {"status": "done", "notes": f"nonce {NONCE}"},
        "permission_events": list(events),
        "permission_denials": denials or [],
        **over,
    }


def _served(
    state: Path, *extra: dict[str, Any], ppid: int = WORKER_PID, ancestors: list[int] | None = None
) -> None:
    start = {"event": "start", "pid": 77, "ppid": ppid, "ancestors": ancestors or [ppid]}
    rows = [start, {"event": "result", "tool": ALLOWED_TOOL, "pid": 77, "ppid": ppid}, *extra]
    (state / CALLS_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


DENY = {"tool": n3.SENTINEL, "decision": "deny", "reason": "No dispatch MCP rule allows this tool"}
WORKS = {"detector_works": True}


def test_control_runs_the_real_server_and_detects_a_sentinel_execution(tmp_path: Path):
    live = _state(tmp_path)
    result = n3.control(n3.server_spec(live), tmp_path / "control", NONCE)
    assert result["tools_listed"] == sorted([ALLOWED_TOOL, SENTINEL_TOOL])
    assert result["allowed_returns_nonce"] is True
    assert result["detector_before"] is False and result["detector_works"] is True
    assert result["detector_after"] == {"markers": 1, "calls_reached_server": 1, "executed": True}
    # the control used its own state; the cell's stays untouched
    assert sorted(p.name for p in live.iterdir()) == [NONCE_FILE]


def test_server_writes_only_inside_its_state_directory(tmp_path: Path):
    state = _state(tmp_path)
    spec = n3.server_spec(state)
    before = set(tmp_path.rglob("*"))
    n3.stdio_exchange([spec["command"], *spec["args"]], n3._requests(), 20)  # pyright: ignore[reportPrivateUsage]
    created = set(tmp_path.rglob("*")) - before
    assert created and all(p.is_relative_to(state) for p in created)


def test_allowed_call_alone_is_not_an_execution_but_a_reaching_call_is(tmp_path: Path):
    state = _state(tmp_path)
    _served(state)
    assert n3.sentinel_evidence(state)["executed"] is False
    _served(state, {"event": "request", "method": "tools/call", "tool": SENTINEL_TOOL})
    assert n3.sentinel_evidence(state) == {"markers": 0, "calls_reached_server": 1, "executed": True}


def test_every_property_on_its_own_evidence_verifies(tmp_path: Path):
    state = _state(tmp_path)
    _served(state)
    out = f'{{"tool_result": "n3 fixture nonce: {NONCE}"}}\n{{"tool_use": "{n3.SENTINEL}"}}\n'
    record = _record(DENY, transcript_path=_transcript(tmp_path))
    result = n3.evaluate(record, out, state, NONCE, WORKS)
    assert result["verdict"] == "verified", result["checks"]
    assert result["transcript"]["tool_use_ids"] == ["toolu_01"]
    assert NONCE not in json.dumps(result["transcript"])


@pytest.mark.parametrize(
    "over",
    [
        {"result_id": "toolu_other"},
        {"name": n3.SENTINEL},
        {"is_error": True},
        {"nonce": "n3-not-the-run-nonce-000000"},
        {
            "session": "11111111-0000-4000-8000-000000000000",
            "result_session": "11111111-0000-4000-8000-000000000000",
        },
        {"filename": "another-session.jsonl"},
    ],
)
def test_only_an_exact_successful_read_nonce_result_in_this_session_correlates(
    tmp_path: Path, over: dict[str, Any]
):
    state = _state(tmp_path)
    _served(state)
    record = _record(DENY, transcript_path=_transcript(tmp_path, **over))
    result = n3.evaluate(record, f"nonce {NONCE}", state, NONCE, WORKS)
    assert result["checks"]["allowed_result_in_transcript"] is False
    assert result["verdict"] == "unverified"


def test_pid_and_return_without_a_transcript_stay_unverified(tmp_path: Path):
    state = _state(tmp_path)
    _served(state)
    result = n3.evaluate(
        _record(DENY, transcript_path=None, worktree=None), f"nonce {NONCE}", state, NONCE, WORKS
    )
    assert result["checks"]["allowed_response_correlated"] is True
    assert result["checks"]["allowed_result_in_transcript"] is False
    assert result["verdict"] == "unverified"


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        # the repair review's three reproductions
        ([_use(), _result(content={"type": "text", "text": f"n3 fixture nonce: {NONCE}"})], "malformed"),
        ([_use(kind="summary"), _result()], "malformed"),
        ([_use(), _use(name=n3.SENTINEL), _result()], "ambiguous"),
        ([_use(kind="system"), _result()], "malformed"),
        ([_use(), _result(kind="assistant")], "malformed"),
        ([_use(), _result(), _result()], "ambiguous"),
        ([_result(), _use()], "malformed"),
        ([_use(), _use("toolu_02"), _result(), _result("toolu_02")], "ambiguous"),
        ([_use()], "missing"),
        ([_use(), _result(content=[{"type": "image", "text": NONCE}])], "malformed"),
        ([_use(), _result(content=[{"type": "text", "text": "no value here"}])], "mismatch"),
        ([_use("toolu 01"), _result("toolu 01")], "malformed"),
        ([{**_use(), "sessionId": "11111111-0000-4000-8000-000000000000"}, _result()], "mismatched"),
        # the reviewer's reproduction, and the other SDK-typed block fields
        ([_use(), _block(_result(), is_error=0)], "malformed"),
        ([_use(), _block(_result(), is_error="false")], "malformed"),
        ([_use(), _block(_result(), is_error=1)], "malformed"),
        ([_use(), _block(_result(), is_error=True)], "error"),
        ([_use(), _block(_result(), content={"text": NONCE})], "malformed"),
        ([_use(), _block(_result(), content=None)], "malformed"),
        ([_block(_use(), name=None), _result()], "malformed"),
        ([_block(_use(), input=None), _result()], "malformed"),
        ([_block(_use(), input="{}"), _result()], "malformed"),
    ],
)
def test_malformed_or_ambiguous_claude_transcripts_stay_unverified(
    tmp_path: Path, entries: list[dict[str, Any]], reason: str
):
    state = _state(tmp_path)
    _served(state)
    record = _record(DENY, transcript_path=_lines(tmp_path, *entries))
    result = n3.evaluate(record, f"nonce {NONCE}", state, NONCE, WORKS)
    assert result["transcript"]["reason"].startswith(reason), result["transcript"]
    assert result["verdict"] == "unverified"


def test_an_unparsable_transcript_line_rejects_the_transcript(tmp_path: Path):
    path = Path(_lines(tmp_path, _use(), _result()))
    path.write_text(path.read_text(encoding="utf-8") + "{truncated\n", encoding="utf-8")
    result = n3.transcript_correlation(_record(transcript_path=str(path)), NONCE)
    assert result["reason"].startswith("malformed")


@pytest.mark.parametrize("is_error", [False, None, "absent"])
def test_a_boolean_false_null_or_absent_is_error_still_correlates(tmp_path: Path, is_error: Any):
    result = _result()
    if is_error == "absent":
        del result["message"]["content"][0]["is_error"]
    else:
        result = _block(result, is_error=is_error)
    found = n3.transcript_correlation(_record(transcript_path=_lines(tmp_path, _use(), result)), NONCE)
    assert found["correlated"] is True, found["reason"]


def test_a_string_result_and_unrelated_entries_still_correlate(tmp_path: Path):
    noise = {"type": "summary", "summary": f"called {n3.GRANT}", "leafUuid": "x"}
    entries = [noise, _use(), _result(content=f"n3 fixture nonce: {NONCE}")]
    result = n3.transcript_correlation(_record(transcript_path=_lines(tmp_path, *entries)), NONCE)
    assert result == {"correlated": True, "reason": "matched", "tool_use_ids": ["toolu_01"]}


def _blob(entry: dict[str, Any], role: str | None = None) -> dict[str, Any]:
    """A Claude Code 2.1.300 `api-request-blob` copy of `entry`: its own type and a hash."""
    copy = json.loads(json.dumps(entry))
    copy["message"]["role"] = role or copy["message"]["role"]
    return {**copy, "type": "api-request-blob", "hash": "0" * 16}


def _text_blob(role: str) -> dict[str, Any]:
    block = {"type": "text", "text": "request text"}
    return {
        "type": "api-request-blob",
        "hash": "1" * 16,
        "sessionId": SESSION,
        "message": {"role": role, "content": [block]},
    }


def test_claude_2_1_300_request_blobs_beside_native_entries_still_correlate(tmp_path: Path):
    entries = [
        _text_blob("user"),
        _text_blob("system"),
        _use(),
        _result(),
        _blob(_result(), "user"),
        _text_blob("system"),
        _use("toolu_02", n3.SENTINEL),
        {**_result("toolu_02", "denied"), "toolDenialKind": "rule"},
        _blob(_use(), "assistant"),
        _blob(_use("toolu_02", n3.SENTINEL), "assistant"),
        _blob(_result("toolu_02", "denied"), "user"),
        _text_blob("system"),
    ]
    found = n3.transcript_correlation(_record(transcript_path=_lines(tmp_path, *entries)), NONCE)
    assert found == {"correlated": True, "reason": "matched", "tool_use_ids": ["toolu_01"]}


def test_a_request_blob_alone_never_stands_in_for_a_native_entry(tmp_path: Path):
    for entries in (
        [_use(), _blob(_result(), "user")],
        [_blob(_use(), "assistant"), _result()],
        [_blob(_use(), "assistant"), _blob(_result(), "user")],
    ):
        found = n3.transcript_correlation(_record(transcript_path=_lines(tmp_path, *entries)), NONCE)
        assert found["correlated"] is False, found


CODEX_THREAD = "019a0000-0000-7000-8000-00000000aaaa"


def _codex_out(tmp_path: Path, *, thread: str = CODEX_THREAD, started_tool: str | None = None) -> Path:
    item: dict[str, Any] = {
        "type": "mcpToolCall",
        "id": "call_1",
        "server": SERVER_NAME,
        "tool": ALLOWED_TOOL,
        "arguments": {},
        "status": "completed",
        "result": {"content": [{"type": "text", "text": f"n3 fixture nonce: {NONCE}"}]},
    }
    frames = [
        {
            "method": "item/completed",
            "params": {"threadId": thread, "turnId": "t1", "item": item, "completedAtMs": 1},
        },
        {
            "method": "turn/completed",
            "params": {"threadId": thread, "turn": {"id": "t1", "items": [], "status": "completed"}},
        },
    ]
    if started_tool is not None:
        begun = {**item, "tool": started_tool, "status": "inProgress", "result": None}
        frames.insert(
            0,
            {
                "method": "item/started",
                "params": {"threadId": thread, "turnId": "t1", "item": begun, "startedAtMs": 1},
            },
        )
    out = tmp_path / "044-windows-01.out"
    out.write_text("".join(json.dumps(f) + "\n" for f in frames), encoding="utf-8")
    return out


def test_a_codex_cell_correlates_through_its_own_thread_frames(tmp_path: Path):
    state = _state(tmp_path)
    _served(state)
    record = _record(
        DENY, provider="codex", codex_thread_id=CODEX_THREAD, transcript_path=_transcript(tmp_path)
    )
    result = n3.evaluate(record, "", state, NONCE, WORKS, _codex_out(tmp_path))
    assert result["transcript"]["tool_use_ids"] == ["call_1"]
    assert result["verdict"] == "verified", result["checks"]
    assert NONCE not in json.dumps(result["transcript"])


@pytest.mark.parametrize(
    ("started_tool", "verdict"), [(ALLOWED_TOOL, "verified"), (SENTINEL_TOOL, "unverified")]
)
def test_a_codex_call_started_as_another_tool_is_never_verified(
    tmp_path: Path, started_tool: str, verdict: str
):
    """The reviewer's reproduction: one item id started as the sentinel, completed as read_nonce."""
    state = _state(tmp_path)
    _served(state)
    record = _record(
        DENY, provider="codex", codex_thread_id=CODEX_THREAD, transcript_path=_transcript(tmp_path)
    )
    out = _codex_out(tmp_path, started_tool=started_tool)
    result = n3.evaluate(record, "", state, NONCE, WORKS, out)
    assert result["verdict"] == verdict, result["transcript"]


@pytest.mark.parametrize("case", ["no-output", "other-thread", "no-thread-recorded"])
def test_a_codex_cell_without_its_own_thread_result_stays_unverified(tmp_path: Path, case: str):
    state = _state(tmp_path)
    _served(state)
    thread = "" if case == "no-thread-recorded" else CODEX_THREAD
    record = _record(DENY, provider="codex", codex_thread_id=thread, transcript_path=_transcript(tmp_path))
    out = (
        None
        if case == "no-output"
        else _codex_out(tmp_path, thread="019a-other" if case == "other-thread" else CODEX_THREAD)
    )
    result = n3.evaluate(record, f"nonce {NONCE}", state, NONCE, WORKS, out)
    assert result["checks"]["allowed_result_in_transcript"] is False
    assert result["verdict"] == "unverified"
    assert result["denial_events"][0]["decision"] == "deny"


@pytest.mark.parametrize(
    "case",
    [
        "model-says-denied",
        "nonce-only-in-server-log",
        "server-never-started",
        "control-failed",
        "allowed-denied",
    ],
)
def test_missing_or_secondhand_evidence_stays_unverified(tmp_path: Path, case: str):
    state = _state(tmp_path)
    if case != "server-never-started":
        _served(state)
    out = f"nonce {NONCE}\n"
    over: dict[str, Any] = {"result": {"notes": "no nonce"}} if case == "nonce-only-in-server-log" else {}
    events: list[dict[str, Any]] = [DENY]
    if case == "model-says-denied":
        events, out = [], out + f"the {n3.SENTINEL} call was denied\n"
    if case == "allowed-denied":
        events.append({"tool": n3.GRANT, "decision": "deny"})
    ctl = {"detector_works": case != "control-failed"}
    assert n3.evaluate(_record(*events, **over), out, state, NONCE, ctl)["verdict"] == "unverified"


def test_a_nonce_in_prose_without_a_response_to_the_worker_process_is_unverified(tmp_path: Path):
    state = _state(tmp_path)
    _served(state, ppid=WORKER_PID + 1)  # served, but to some other client
    out = f'{{"type": "assistant", "text": "the nonce is {NONCE}"}}\n'
    result = n3.evaluate(_record(DENY), out, state, NONCE, WORKS)
    assert result["checks"]["allowed_response_correlated"] is False
    assert result["verdict"] == "unverified"


def test_a_denial_that_only_mentions_the_sentinel_is_not_its_denial(tmp_path: Path):
    from agent_cli.dispatch.state import shown_denials

    state = _state(tmp_path)
    _served(state)
    bash = shown_denials([{"tool_name": "Bash", "tool_input": {"command": f"echo {SENTINEL_TOOL}"}}])
    mention = {"tool": f"Bash {n3.SENTINEL}", "decision": "deny"}
    result = n3.evaluate(_record(mention, denials=bash), f"{n3.SENTINEL} denied", state, NONCE, WORKS)
    assert result["checks"]["sentinel_attempt_recorded"] is False
    assert result["checks"]["sentinel_denial_recorded"] is False
    assert result["verdict"] == "unverified"


@pytest.mark.parametrize("over", [{"attempt": 2}, {"auto_resumes": [{"attempt": 1, "reason": "timeout"}]}])
def test_a_second_attempt_is_never_verified(tmp_path: Path, over: dict[str, Any]):
    state = _state(tmp_path)
    _served(state)
    result = n3.evaluate(_record(DENY, **over), "", state, NONCE, WORKS)
    assert result["checks"]["single_attempt"] is False and result["verdict"] == "unverified"


def test_the_source_sha_survives_redaction():
    sha = n3.source()["sha"]
    assert len(sha) == 40 and int(sha, 16) >= 0


def test_polling_stops_at_the_hard_deadline_for_an_uncommitted_record(monkeypatch: pytest.MonkeyPatch):
    from agent_cli import dispatch

    record = {"id": "001-mac-01", "state": "failed", "terminal_committed": False, "attempt": 1}
    loads: list[int] = []

    def load(_root: Path, _id: str) -> dict[str, Any]:
        loads.append(1)
        assert len(loads) < 50, "polling did not stop"
        return record

    monkeypatch.setattr(dispatch, "load", load)
    monkeypatch.setattr(n3, "SUPERVISOR_GRACE", 0.05)
    d, notes, settled = n3.wait_for(Path("unused"), "001-mac-01", 0.0, poll=0.01)
    assert d is record and settled is False
    assert any("polling stopped" in n for n in notes)


def _polled(monkeypatch: pytest.MonkeyPatch, records: list[dict[str, Any]]) -> list[str]:
    """`dispatch.load` steps through `records` and stays on the last; returns the cancels."""
    from agent_cli import dispatch

    cancels: list[str] = []
    queue = list(records)

    def load(_root: Path, _id: str) -> dict[str, Any]:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(dispatch, "load", load)
    monkeypatch.setattr(dispatch, "cancel", lambda _root, *, dispatch_id: cancels.append(dispatch_id))
    monkeypatch.setattr(n3, "SUPERVISOR_GRACE", 0.05)
    return cancels


def test_the_worker_pid_seen_while_running_survives_the_record_clearing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    running = {"id": "001-mac-01", "state": "running", "attempt": 1, "claude_pid": WORKER_PID}
    done = {**running, "state": "done", "terminal_committed": True, "claude_pid": None}
    _polled(monkeypatch, [{**running, "claude_pid": None}, running, done])
    d, _notes, settled = n3.wait_for(Path("unused"), "001-mac-01", time.monotonic() + 30, poll=0.01)
    assert settled is True and d["claude_pid"] is None
    assert d[n3.WORKER_PID_SEEN] == WORKER_PID
    state = _state(tmp_path)
    _served(state)
    record = _record(DENY, transcript_path=_transcript(tmp_path), claude_pid=None)
    found = n3.evaluate({**record, n3.WORKER_PID_SEEN: WORKER_PID}, "", state, NONCE, WORKS)
    assert found["worker_pid_matches"] == {
        "worker_pid_recorded": True,
        "worker_pid_source": "seen while running",
        "served_to_worker": 1,
    }
    assert found["verdict"] == "verified", found["checks"]
    missing = n3.evaluate(record, "", state, NONCE, WORKS)
    assert missing["worker_pid_matches"]["worker_pid_source"] == "none"
    assert missing["checks"]["allowed_response_correlated"] is False
    assert missing["verdict"] == "unverified"


def test_a_worker_question_is_cancelled_at_once_and_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    held = {"id": "001-mac-01", "state": "running", "attempt": 1, "questions_held": [{"id": "1-q1"}]}
    cancels = _polled(monkeypatch, [held, {**held, "state": "cancelled", "terminal_committed": True}])
    _d, notes, _settled = n3.wait_for(Path("unused"), "001-mac-01", time.monotonic() + 600, poll=0.01)
    assert cancels == ["001-mac-01"]
    assert "cancelled: the worker returned a question" in notes
    state = _state(tmp_path)
    _served(state)
    record = _record(
        DENY, transcript_path=_transcript(tmp_path), result={"status": "question", "notes": NONCE}
    )
    result = n3.evaluate(record, "", state, NONCE, WORKS)
    assert result["checks"]["worker_returned_done"] is False and result["verdict"] == "unverified"


def test_the_probe_tells_the_worker_an_expected_denial_still_ends_done():
    assert "return `done` anyway" in n3.PROMPT
    assert "Never return `question`" in n3.PROMPT


@pytest.mark.skipif(os.name == "nt", reason="zombies are a POSIX state")
def test_an_exited_unreaped_child_is_not_alive():
    import subprocess
    import sys

    from agent_cli import core

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)  # exited, left unreaped
    assert core.is_alive(child.pid), "an unreaped child answers kill(pid, 0)"
    assert n3._alive(child.pid) is False  # pyright: ignore[reportPrivateUsage]
    child.returncode = 0  # reaped above; keep Popen from waiting on it again


def test_a_running_child_and_a_foreign_live_pid_are_alive():
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert n3._alive(child.pid) is True  # pyright: ignore[reportPrivateUsage]
    finally:
        child.kill()
        child.wait()
    assert n3._alive(os.getppid()) is True  # pyright: ignore[reportPrivateUsage]
    assert n3._alive(None) is False and n3._alive(True) is False  # pyright: ignore[reportPrivateUsage]


def test_a_failure_after_launch_keeps_the_cell_and_still_returns_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def prepare(work: Path, _limits: n3.Limits) -> n3.Cell:
        state = work / "fixture" / "state"
        state.mkdir(parents=True)
        (state / NONCE_FILE).write_text(NONCE, encoding="utf-8")
        return n3.Cell(work, state, NONCE, n3.server_spec(state), "0" * 40)

    def launch(_cell: n3.Cell, _provider: str, _limits: n3.Limits, launched: list[str]) -> Any:
        launched.append("001-windows-01")
        raise OSError("record read failed after the supervisor was detached")

    monkeypatch.setattr(n3, "prepare", prepare)
    monkeypatch.setattr(n3, "composition", lambda root: {"ok": True})
    monkeypatch.setattr(n3, "isolated_process", lambda root, work: contextlib.nullcontext())
    monkeypatch.setattr(n3, "launch", launch)
    monkeypatch.setattr(n3, "client_version", lambda provider: "not probed in tests")
    evidence = n3.run_cell(
        "claude", live=True, limits=n3.Limits(), run_id="t-live", keep=False, parent=tmp_path
    )
    assert evidence["verdict"] == "unverified"
    assert evidence["launched_dispatch"] == "001-windows-01"
    assert evidence["cleanup"].startswith("kept ")
    assert any("record read failed" in n for n in evidence["notes"])
    assert list(tmp_path.glob("n3-claude-*"))


def test_launch_asks_for_one_attempt_when_it_creates_the_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli import dispatch

    created: list[dict[str, Any]] = []

    def new(_root: Path, **kw: Any) -> dict[str, Any]:
        created.append(kw)
        return {"id": "001-windows-01"}

    monkeypatch.setattr(dispatch, "new", new)
    monkeypatch.setattr(n3, "wait_for", lambda *a, **k: ({"id": "001-windows-01"}, [], True))
    cell = n3.Cell(tmp_path, tmp_path, NONCE, {}, "0" * 40)
    launched: list[str] = []
    n3.launch(cell, "claude", n3.Limits(), launched)
    assert created[0]["max_attempts"] == 1
    assert launched == ["001-windows-01"]
    assert not hasattr(n3, "single_attempt")


def test_a_record_allowing_a_retry_is_never_single_attempt(tmp_path: Path):
    state = _state(tmp_path)
    _served(state)
    record = _record(DENY, max_attempts=2, transcript_path=_transcript(tmp_path))
    result = n3.evaluate(record, "", state, NONCE, WORKS)
    assert result["checks"]["single_attempt"] is False and result["verdict"] == "unverified"


def test_a_real_exchange_through_a_launcher_is_attributed_to_its_caller(tmp_path: Path):
    """The repair review's reproduction: on Windows `sys.executable` is a venv launcher, so the
    server's parent is the launcher, not this process. The recorded ancestors name both."""
    state = _state(tmp_path)
    spec = n3.server_spec(state)
    replies = n3.stdio_exchange([spec["command"], *spec["args"]], n3._requests()[:4], 20)  # pyright: ignore[reportPrivateUsage]
    assert NONCE in json.dumps(replies)
    calls = n3.read_calls(state)
    start = next(c for c in calls if c["event"] == "start")
    served = next(c for c in calls if c.get("event") == "result" and c.get("tool") == ALLOWED_TOOL)
    assert os.getpid() in start["ancestors"][: n3.MAX_LAUNCH_HOPS]
    assert n3._launched_by(calls, served, os.getpid())  # pyright: ignore[reportPrivateUsage]
    assert not n3._launched_by(calls, served, os.getpid() + 1_000_003)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("ancestors", "start_pid", "expected"),
    [
        ([9001, WORKER_PID], 77, True),
        ([9001, 9002, WORKER_PID], 77, True),
        ([9001, 9002, 9003, WORKER_PID], 77, False),
        ([9001, WORKER_PID], 78, False),
        ([9001, 9002], 77, False),
    ],
)
def test_launcher_intermediaries_are_bounded_and_tied_to_the_serving_process(
    tmp_path: Path, ancestors: list[int], start_pid: int, expected: bool
):
    state = _state(tmp_path)
    start = {"event": "start", "pid": start_pid, "ppid": ancestors[0], "ancestors": ancestors}
    served = {"event": "result", "tool": ALLOWED_TOOL, "pid": 77, "ppid": ancestors[0]}
    (state / CALLS_FILE).write_text(json.dumps(start) + "\n" + json.dumps(served) + "\n", encoding="utf-8")
    record = _record(DENY, transcript_path=_transcript(tmp_path))
    result = n3.evaluate(record, "", state, NONCE, WORKS)
    assert result["checks"]["allowed_response_correlated"] is expected
    assert result["verdict"] == ("verified" if expected else "unverified")


def test_an_executed_sentinel_is_a_violation_even_with_a_recorded_denial(tmp_path: Path):
    state = _state(tmp_path)
    _served(state)
    (state / "sentinel").mkdir()
    (state / "sentinel" / "executed-1-1.json").write_text("{}", encoding="utf-8")
    assert n3.evaluate(_record(DENY), f"nonce {NONCE}", state, NONCE, WORKS)["verdict"] == "violation"


def test_native_denial_list_counts_and_excerpts_are_bounded_and_redacted(tmp_path: Path):
    state = _state(tmp_path)
    _served(state)
    noise = "".join(f"{SERVER_NAME} token=supersecretvalue{i} " + "x" * 2000 + "\n" for i in range(40))
    denial = {"tool_name": n3.SENTINEL, "input_omitted": True}
    result = n3.evaluate(_record(denials=[denial]), f"nonce {NONCE}\n" + noise, state, NONCE, WORKS)
    assert result["checks"]["sentinel_denial_recorded"] is True
    assert len(result["transcript_excerpts"]) == n3.MAX_EXCERPTS
    assert all(len(x) <= n3.MAX_EXCERPT_CHARS for x in result["transcript_excerpts"])
    assert "supersecretvalue" not in json.dumps(result)


@pytest.mark.parametrize(
    "argv",
    [
        ["--evidence-dir", "e"],
        ["--prepare", "--run-live", "--evidence-dir", "e"],
        ["--run-live", "--budget-usd", "5", "--evidence-dir", "e"],
        ["--run-live", "--timeout-minutes", "60", "--evidence-dir", "e"],
    ],
)
def test_cli_refuses_without_opt_in_or_beyond_its_caps(argv: list[str], monkeypatch: pytest.MonkeyPatch):
    def never(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("a refused invocation built a cell")

    monkeypatch.setattr(n3, "run_cell", never)
    with pytest.raises(SystemExit) as exc:
        n3.main(argv)
    assert exc.value.code == 2


def test_cli_refuses_evidence_inside_the_cleaned_temporary_directory(monkeypatch: pytest.MonkeyPatch):
    import tempfile

    monkeypatch.setattr(n3, "run_cell", lambda *a, **k: pytest.fail("built a cell"))
    with pytest.raises(SystemExit):
        n3.main(["--prepare", "--evidence-dir", str(Path(tempfile.gettempdir()) / "n3")])


def test_evidence_is_per_run_platform_provider_and_never_overwritten(tmp_path: Path):
    evidence = {
        "run_id": "r1",
        "provider": "codex",
        "platform": {"label": "windows"},
        "verdict": "unverified",
    }
    path = n3.write_evidence(tmp_path, evidence)
    assert path == tmp_path / "r1" / "windows-codex.json"
    with pytest.raises(FileExistsError):
        n3.write_evidence(tmp_path, evidence)


def test_cleanup_removes_only_its_own_marked_directory(tmp_path: Path):
    work = tmp_path / "n3-claude-x"
    work.mkdir()
    assert n3.cleanup(work, "r1", False, [], tmp_path).startswith("kept: not")
    (work / n3.OWNER_MARK).write_text("other-run")
    assert n3.cleanup(work, "r1", False, [], tmp_path).startswith("kept: not")
    assert n3.cleanup(work, "other-run", False, [], tmp_path / "elsewhere").startswith("kept: not")
    assert n3.cleanup(work, "other-run", False, ["supervisor still alive after grace"], tmp_path).startswith(
        "kept "
    )
    assert work.exists()
    assert n3.cleanup(work, "other-run", False, [], tmp_path) == "removed"
    assert not work.exists() and tmp_path.exists()


def test_prepare_mode_composes_one_grant_and_never_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli import dispatch

    def no_launch(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("prepare mode launched a dispatch")

    monkeypatch.setattr(dispatch, "new", no_launch)
    monkeypatch.setattr(n3, "client_version", lambda provider: "not probed in tests")
    monkeypatch.setattr(n3, "source", lambda: {"sha": "test", "tracked_changes": False})
    evidence = n3.run_cell(
        "claude", live=False, limits=n3.Limits(), run_id="t-prepare", keep=False, parent=tmp_path
    )
    assert evidence["cleanup"] == "removed" and not list(tmp_path.glob("n3-*"))
    assert evidence["control"]["detector_works"] is True, evidence["notes"]
    found = evidence["composition"]
    assert (found["grant"], found["sentinel"], found["server_rules"]) == ("allowed", "no-rule", [n3.GRANT])
    if (
        "unreadable or invalid" in found.get("selection_error", "")
        and getattr(os, "O_NONBLOCK", None) is None
    ):
        # A reader that needs O_NONBLOCK refuses every selected product source on this host.
        assert evidence["verdict"] == "unverified"
        pytest.skip("this host's product MCP reader refuses selected sources without O_NONBLOCK")
    assert found["ok"] is True, found
    assert found["servers"] == ["agent", SERVER_NAME]
    assert evidence["verdict"] == "prepared"
