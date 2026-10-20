"""When the transcript scan is behind, or met an
entry too long to parse, the next session start or prompt says in one line why ⏳ may be stuck,
and says nothing once that clears."""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli import sessions
from agent_cli.hooks import session as session_hooks

SID = "s1"


def _context(raw: str) -> str:
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw else ""


def _stuck_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "may be stuck" in line]


def test_a_scan_that_is_behind_or_met_an_oversized_entry_is_told_in_one_line(tmp_root: Path):
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, "background_tasks", {"bbig": "t", "bslow": "t"})
    sessions.set_field(
        tmp_root,
        SID,
        "transcript_scan",
        {
            "path": "/t.jsonl",
            "offset": 9000,
            "behind": 5000,
            "unparsed": [{"from": 0, "to": 7000, "ids": ["bbig"]}, {"from": 7000, "to": 8000, "ids": []}],
        },
    )
    for raw in (
        session_hooks.session_start(tmp_root, {"session_id": SID, "source": "startup"}),
        session_hooks.dispatch_inbox(tmp_root, {"session_id": SID, "prompt": "go on"}),
    ):
        lines = _stuck_lines(_context(raw))
        assert len(lines) == 1
        assert "5000 transcript bytes" in lines[0]
        assert "bytes 0-7000" in lines[0] and "bbig" in lines[0]
        assert "7000-8000" not in lines[0]  # nothing finished was seen in that one


def test_the_line_clears_once_the_state_clears(tmp_root: Path):
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    sessions.set_field(
        tmp_root,
        SID,
        "transcript_scan",
        {
            "path": "/t.jsonl",
            "offset": 9000,
            "behind": 3,
            "unparsed": [{"from": 0, "to": 7000, "ids": ["bbig"]}],
        },
    )
    # nothing outstanding: a few unread bytes and an old oversized entry hold no ⏳
    sessions.set_field(tmp_root, SID, "background_tasks", {})
    assert _stuck_lines(_context(session_hooks.session_start(tmp_root, {"session_id": SID}))) == []
    assert session_hooks.dispatch_inbox(tmp_root, {"session_id": SID, "prompt": "go on"}) == ""

    sessions.set_field(tmp_root, SID, "background_tasks", {"bother": "t"})
    sessions.set_field(tmp_root, SID, "transcript_scan", {"path": "/t.jsonl", "offset": 9000})
    assert session_hooks.dispatch_inbox(tmp_root, {"session_id": SID, "prompt": "go on"}) == ""
