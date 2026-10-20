"""A steer sent while the supervisor is writing the same record (task 45).

`dispatch say` loaded the record and saved it without the record lock the supervisor's writers
hold. When a supervisor write landed in between, the save failed with "stale dispatch record"
and the steer never reached the thread or the handoff.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from agent_cli import dispatch
from agent_cli.core import AgentError
from agent_cli.dispatch import state, supervisor


def _running(root: Path) -> None:
    state.save(
        root,
        {
            "id": "001-01",
            "repo": "alpha",
            "short": "alpha",
            "state": "running",
            "supervisor_pid": os.getpid(),
            "thread": [],
        },
    )


def test_a_steer_lands_while_the_supervisor_writes_the_record(tmp_root: Path):
    _running(tmp_root)
    stop = threading.Event()
    errors: list[BaseException] = []

    def supervisor_writes() -> None:
        tokens = 0
        while not stop.is_set():
            tokens += 1
            try:
                supervisor.persist_tokens(tmp_root, "001-01", tokens)
            except BaseException as exc:  # noqa: BLE001 — surfaced by the assertion below
                errors.append(exc)
                return

    writer = threading.Thread(target=supervisor_writes, daemon=True)
    writer.start()
    try:
        steers = [f"steer {n}" for n in range(30)]
        for text in steers:
            dispatch.say(tmp_root, dispatch_id="001-01", message=text)
    finally:
        stop.set()
        writer.join(timeout=10)
    assert errors == []
    record = dispatch.load(tmp_root, "001-01")
    assert [entry["text"] for entry in record["thread"] if entry.get("steer")] == steers
    assert record["tokens"] > 0


def test_worker_stop_writes_the_transcript_without_losing_supervisor_fields(tmp_root: Path, monkeypatch):
    """worker_stop now writes under the same record lock; it must still keep other fields."""
    _running(tmp_root)
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-01")
    supervisor.persist_tokens(tmp_root, "001-01", 5)
    supervisor.worker_stop(tmp_root, {"session_id": "s", "transcript_path": "/tmp/t.jsonl"})
    record = dispatch.load(tmp_root, "001-01")
    assert record["transcript_path"] == "/tmp/t.jsonl" and record["tokens"] == 5


def test_worker_stop_tolerates_a_missing_record_but_not_a_failed_write(tmp_root: Path, monkeypatch):
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-01")
    assert supervisor.worker_stop(tmp_root, {"session_id": "s", "transcript_path": "/tmp/t.jsonl"}) == ""
    _running(tmp_root)

    def refuse(root, d):
        raise AgentError("disk full")

    monkeypatch.setattr(supervisor, "save", refuse)
    with pytest.raises(AgentError, match="disk full"):
        supervisor.worker_stop(tmp_root, {"session_id": "s", "transcript_path": "/tmp/t.jsonl"})
