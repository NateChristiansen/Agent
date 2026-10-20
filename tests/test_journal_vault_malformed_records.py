from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from agent_cli import core, dispatch, sessions
from agent_cli.core import iso, now
from agent_cli.journal import block
from agent_cli.vault import live, managers

DAY = date(2026, 9, 3)  # the fixed clock's today()


def test_dispatches_today_reads_a_result_that_is_not_an_object_as_missing(tmp_root: Path):
    (tmp_root / ".agent/dispatches").mkdir(parents=True)
    record = {"id": "999-01", "state": "failed", "finished": iso(now()), "result": "junk", "error": "boom"}
    dispatch.save(tmp_root, record)
    [row] = block.dispatches_today(tmp_root, now().astimezone().date())
    assert row["summary"] == "boom"


def test_a_claude_session_whose_pid_is_a_string_is_not_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    entry = {"pid": str(os.getpid()), "sessionId": "s1"}
    (claude / "1.json").write_text(json.dumps(entry), encoding="utf-8")
    assert managers.claude_live_sessions() == {}


@pytest.fixture
def no_claude_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = tmp_path / "claude" / "sessions"
    claude.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.parent))
    monkeypatch.setattr(core, "is_alive", lambda pid: True)


@pytest.mark.usefixtures("no_claude_list")
def test_a_manager_activity_that_is_not_an_object_reads_as_idle(tmp_root: Path):
    sessions.set_record(tmp_root, "s1", name="mgr-a", pid=1)
    sessions.set_field(tmp_root, "s1", "activity", "junk")
    [row] = managers.rows(tmp_root, dispatches=[], closed_features=set(), held=[])
    assert row["activity"] == "idle"


@pytest.mark.usefixtures("no_claude_list")
@pytest.mark.parametrize(("raw", "shown"), [("33", "033 x"), ("033", "033 x"), ("abc", "— x"), (33.5, "— x")])
def test_a_manager_feature_id_that_is_not_an_int_shows_its_digits_or_a_placeholder(
    tmp_root: Path, raw: object, shown: str
):
    sessions.set_record(tmp_root, "s1", name="mgr-a", pid=1, feature={"id": raw, "slug": "x"})
    [row] = managers.rows(tmp_root, dispatches=[], closed_features=set(), held=[])
    assert row["feature"] == shown


def test_a_tools_value_that_is_not_a_list_names_no_tool(tmp_path: Path):
    out = tmp_path / "w.out"
    out.write_text(json.dumps({"type": "assistant", "tools": "Bash"}) + "\n", encoding="utf-8")
    assert live.last_tool(out) is None
