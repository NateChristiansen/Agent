"""`capture` on SessionStart for both clients.

Five live Codex /clear and resume runs could not show when SessionStart ran, because no client
wired `capture` on it, although the capture code always mapped the event."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("notes_remote")


@pytest.mark.parametrize(
    "payload",
    [
        # Claude Code's SessionStart: `source` is startup, resume, clear or compact
        {
            "session_id": "s1",
            "hook_event_name": "SessionStart",
            "source": "resume",
            "transcript_path": "/t.jsonl",
        },
        {"session_id": "s1", "hook_event_name": "SessionStart", "source": "clear", "duration_ms": 41},
        # Codex's SessionStart carries the same names; its `source` after /clear is `clear`
        {"session_id": "s1", "hook_event_name": "SessionStart", "source": "clear", "model": "gpt-5.5-codex"},
        {"session_id": "s1", "hook_event_name": "SessionStart", "source": "resume", "durationMs": 7},
    ],
)
def test_capture_hook_records_session_start_with_its_source(tmp_root: Path, run, payload):
    """The record keeps `source` and any duration the client supplies, unchanged, in `extra`."""
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    r = run(["hook", "capture"], stdin=json.dumps(payload))
    assert r.code == 0 and r.out == ""
    lines = (tmp_root / ".agent/raw/001-x/2026-09-03.jsonl").read_text().splitlines()
    record = json.loads(lines[-1])
    assert record["kind"] == "session-start"
    extra = {
        k: v for k, v in payload.items() if k not in ("session_id", "hook_event_name", "transcript_path")
    }
    assert record["extra"] == extra


def test_both_clients_wire_capture_on_session_start():
    from agent_cli.hooks.groups import wired

    repo = Path(__file__).resolve().parents[1]
    for config in (repo / ".claude/settings.json", repo / ".codex/hooks.json"):
        assert "capture" in wired(json.loads(config.read_text()), "SessionStart")
