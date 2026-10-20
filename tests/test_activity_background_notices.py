"""A bug: a finished background task whose notice never arrived as a typed prompt
left the manager's tab on ⏳ while it sat idle (2026-09-30). The notice came mid-turn as a
`queued_command` attachment, or as context injected beside an owner prompt. Stop now reads the
transcript written since its last scan and closes each task a finished notice names."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import hooks, sessions, tab_drain

SID = "s1"


def _notice(task_id: str, status: str | None = "completed") -> str:
    status_tag = f"<status>{status}</status>\n" if status else ""
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>toolu_x</tool-use-id>\n{status_tag}<summary>done</summary>\n</task-notification>"
    )


def _queued_command(task_id: str) -> dict:
    """The mid-turn delivery, as Claude Code 2.1.287 writes it."""
    return {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": _notice(task_id),
            "commandMode": "task-notification",
        },
    }


def _beside_prompt(task_id: str) -> dict:
    """The delivery injected as context next to a prompt the owner typed."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": _notice(task_id)}, {"type": "text", "text": "carry on"}],
        },
    }


def _append(path: Path, *entries: dict) -> None:
    with path.open("a", encoding="utf-8", newline="") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _setup(tmp_root: Path, monkeypatch, pending: dict[str, str]) -> list[str]:
    sessions.set_record(tmp_root, SID, pid=4242, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, "activity", {"state": "working"})
    sessions.set_field(tmp_root, SID, "background_tasks", pending)
    labels: list[str] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: labels.append(label) or {"started": True},
    )
    return labels


def _stop(tmp_root: Path, transcript: Path) -> None:
    hooks.activity(
        tmp_root, {"session_id": SID, "hook_event_name": "Stop", "transcript_path": str(transcript)}
    )


def test_stop_closes_tasks_whose_notices_arrived_outside_a_typed_prompt(
    tmp_root: Path, monkeypatch, tmp_path
):
    transcript = tmp_path / "t.jsonl"
    _append(transcript, {"type": "user", "message": {"role": "user", "content": "start two"}})
    labels = _setup(tmp_root, monkeypatch, {"a554bb00fa2d78815": "t", "b9fmq5zfx": "t"})
    _append(transcript, _queued_command("a554bb00fa2d78815"), _beside_prompt("b9fmq5zfx"))

    _stop(tmp_root, transcript)

    record = sessions.get(tmp_root, SID) or {}
    assert record["background_tasks"] == {}
    assert labels[-1] == "✅ mgr-008-x · cl s1"


def test_stop_scans_only_new_bytes_and_only_finished_notices(tmp_root: Path, monkeypatch, tmp_path):
    """A Monitor reports each event with its `<task-id>` and no `<status>`; that is not the end
    of it. A notice quoted in a tool result is not a delivery. And bytes read by one Stop are
    not read again, so a hook never rescans a long transcript."""
    transcript = tmp_path / "t.jsonl"
    labels = _setup(tmp_root, monkeypatch, {"bmonitor1": "t"})
    _append(
        transcript,
        {"type": "queue-operation", "operation": "enqueue", "content": _notice("bmonitor1", status=None)},
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": _notice("bmonitor1")}],
            },
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _notice("bmonitor1")}]},
        },
        {"type": "queue-operation", "operation": "enqueue", "content": _notice("bold")},
    )
    _stop(tmp_root, transcript)
    record = sessions.get(tmp_root, SID) or {}
    assert record["background_tasks"] == {"bmonitor1": "t"}
    assert labels[-1] == "⏳ mgr-008-x · cl s1"
    assert record["transcript_scan"] == {"path": str(transcript), "offset": transcript.stat().st_size}

    # "bold" finished before the last scan; its notice is not read again.
    sessions.set_field(tmp_root, SID, "background_tasks", {"bmonitor1": "t", "bold": "t"})
    _append(transcript, {"type": "queue-operation", "operation": "enqueue", "content": _notice("bmonitor1")})
    _stop(tmp_root, transcript)
    assert (sessions.get(tmp_root, SID) or {})["background_tasks"] == {"bold": "t"}


def test_stop_survives_a_missing_or_partial_transcript(tmp_root: Path, monkeypatch, tmp_path):
    """A hook never raises. A missing file changes nothing; a line still being written is left
    for the next scan instead of being skipped."""
    labels = _setup(tmp_root, monkeypatch, {"bx": "t"})
    _stop(tmp_root, tmp_path / "missing.jsonl")
    assert (sessions.get(tmp_root, SID) or {})["background_tasks"] == {"bx": "t"}

    transcript = tmp_path / "t.jsonl"
    line = json.dumps(_queued_command("bx"))
    transcript.write_text(line[:20], encoding="utf-8")
    _stop(tmp_root, transcript)
    assert (sessions.get(tmp_root, SID) or {})["background_tasks"] == {"bx": "t"}
    transcript.write_text(line + "\n", encoding="utf-8", newline="")
    _stop(tmp_root, transcript)
    assert (sessions.get(tmp_root, SID) or {})["background_tasks"] == {}
    assert labels[-1].startswith("✅")


def test_a_backlog_larger_than_one_scan_is_read_over_later_stops(tmp_root: Path, monkeypatch, tmp_path):
    """Review of the first fix: a scan that jumped to the newest bytes dropped the backlog's
    older end for good, with any notice in it. Each Stop now reads a bounded chunk from where
    the last one ended, and never moves the mark past bytes it has not read."""
    from agent_cli.hooks import task_notices

    monkeypatch.setattr(task_notices, "MAX_SCAN_BYTES", 2000)
    _setup(tmp_root, monkeypatch, {"bold": "t", "bmiddle": "t"})
    filler = {"type": "assistant", "message": {"role": "assistant", "content": "x" * 80}}
    transcript = tmp_path / "t.jsonl"
    _append(transcript, _queued_command("bold"), *[filler] * 30, _queued_command("bmiddle"), *[filler] * 30)
    size = transcript.stat().st_size
    assert size > 3 * task_notices.MAX_SCAN_BYTES

    _stop(tmp_root, transcript)
    record = sessions.get(tmp_root, SID) or {}
    assert record["background_tasks"] == {"bmiddle": "t"}  # the oldest notice, read first
    mark = record["transcript_scan"]
    assert 0 < mark["offset"] <= task_notices.MAX_SCAN_BYTES
    # The rest waits for later Stops, and the record says how much: an idle manager keeps ⏳
    # until then, and nothing pretends otherwise.
    assert mark["behind"] == size - mark["offset"]

    for _ in range(10):
        _stop(tmp_root, transcript)
    record = sessions.get(tmp_root, SID) or {}
    assert record["background_tasks"] == {}
    assert record["transcript_scan"]["offset"] == size


def test_only_a_terminal_status_closes_a_task():
    """Claude Code wrote `completed` and `failed` in this machine's transcripts; `killed` is an
    unverified assumption for a stopped task, never seen written. A Monitor event has no status
    and the task runs on."""
    from agent_cli.hooks.task_notices import finished_ids

    def notice(status_tag: str) -> str:
        return f"<task-notification>\n<task-id>b1</task-id>\n{status_tag}\n</task-notification>"

    for status in ("completed", "failed", "killed"):
        assert finished_ids(notice(f"<status>{status}</status>")) == {"b1"}
    for tag in (
        "",
        "<status>running</status>",
        "<status></status>",
        "<status>completed",
        "<status>done?</status>",
    ):
        assert finished_ids(notice(tag)) == set()


def _oversized(tmp_root: Path, monkeypatch, tmp_path, pending: dict[str, str], *entries: dict):
    from agent_cli.hooks import task_notices

    monkeypatch.setattr(task_notices, "MAX_SCAN_BYTES", 2000)
    _setup(tmp_root, monkeypatch, pending)
    transcript = tmp_path / "t.jsonl"
    _append(transcript, *entries)
    for _ in range(8):
        _stop(tmp_root, transcript)
    return transcript, sessions.get(tmp_root, SID) or {}


def test_an_oversized_delivered_entry_is_recorded_as_unread_not_passed_over(
    tmp_root: Path, monkeypatch, tmp_path
):
    """an owner prompt with a huge paste can carry the injected completion
    notice, and an entry longer than one bounded read cannot be parsed to show it was delivered.
    The task stays open, so the tab keeps ⏳, and the record names the unread entry and the ids
    seen in it, instead of the scan passing over it in silence."""
    big = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "x" * 5000 + _notice("bbig")}]},
    }
    line = len(json.dumps(big)) + 1
    _, record = _oversized(tmp_root, monkeypatch, tmp_path, {"bbig": "t"}, big)
    assert record["background_tasks"] == {"bbig": "t"}
    assert record["transcript_scan"]["unparsed"] == [{"from": 0, "to": line, "ids": ["bbig"]}]


def test_an_oversized_tool_output_does_not_hide_the_next_notice(tmp_root: Path, monkeypatch, tmp_path):
    huge = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "y" * 5000}]},
    }
    line = len(json.dumps(huge)) + 1
    transcript, record = _oversized(
        tmp_root, monkeypatch, tmp_path, {"bnext": "t"}, huge, _queued_command("bnext")
    )
    assert record["background_tasks"] == {}
    assert record["transcript_scan"]["offset"] == transcript.stat().st_size
    assert record["transcript_scan"]["unparsed"] == [{"from": 0, "to": line, "ids": []}]


def test_a_monitor_event_typed_as_a_prompt_does_not_close_its_task():
    from agent_cli.hooks.task_waits import pending_after

    event = "<task-notification>\n<task-id>bmon</task-id>\n<summary>Monitor event: tick</summary>\n</task-notification>"
    assert pending_after({"hook_event_name": "UserPromptSubmit", "prompt": event}, {"bmon": "t"}) is None
    done = pending_after(
        {"hook_event_name": "UserPromptSubmit", "prompt": _notice("bmon")}, {"bmon": "t", "other": "t"}
    )
    assert done == {"other": "t"}


@pytest.mark.parametrize("bad_ids", [5, "bxyz", {"a": 1}, [1, None]])
def test_a_malformed_carried_id_list_in_the_mark_is_dropped_not_fatal(tmp_path: Path, bad_ids):
    from agent_cli.hooks import task_notices

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("partial line, no newline yet", encoding="utf-8")
    mark = {"path": str(transcript), "offset": 0, "in_line": {"from": 0, "ids": bad_ids, "carry": ""}}
    finished, new_mark = task_notices.scan(str(transcript), mark)
    assert finished == set()
    assert new_mark is not None
    assert new_mark["in_line"]["ids"] == []
