"""One machine's day of harness usage, collected by cwd from Claude transcripts and Codex rollouts."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_cli.usage import collect_day, record_problems

DAY = date(2026, 9, 3)


@pytest.fixture(autouse=True)
def _no_codex_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)


def _stamp(day: int, hour: int, minute: int = 0) -> str:
    return (
        datetime(2026, 9, day, hour, minute).astimezone().astimezone(UTC).isoformat().replace("+00:00", "Z")
    )


def _assistant(
    cwd: Path, session: str, request: str, tokens: int, *, day: int = 3, hour: int = 12, **extra: Any
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "cwd": str(cwd),
        "sessionId": session,
        "requestId": request,
        "timestamp": _stamp(day, hour),
        "message": {
            "model": "claude-opus-5-5",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": tokens - 6,
            },
        },
        **extra,
    }


def _write(path: Path, *records: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _project(home: Path, folder: str) -> Path:
    return home / ".claude" / "projects" / folder


def _dispatch(root: Path, dispatch_id: str, **fields: Any) -> None:
    record = {
        "id": dispatch_id,
        "kind": "work",
        "row": "build",
        "provider": "claude",
        "tokens": 999_999,
        **fields,
    }
    _write(root / ".agent" / "dispatches" / f"{dispatch_id}.json", record)


def _rollout(
    sessions: Path,
    name: str,
    thread: str,
    cwd: Path,
    *usage: tuple[str, int],
    parent: str = "",
    spawn: tuple[str, str] | None = None,
    flat: bool = False,
) -> Path:
    meta: dict[str, Any] = {"id": thread, "cwd": str(cwd)}
    if parent:
        meta["parent_thread_id"] = parent
    if spawn:
        meta["source"] = {
            "subagent": {"thread_spawn": {"parent_thread_id": spawn[0], "agent_path": spawn[1]}}
        }
    rows: list[dict[str, Any]] = [
        {"type": "session_meta", "timestamp": _stamp(3, 9), "payload": meta},
        {
            "type": "turn_context",
            "timestamp": _stamp(3, 9),
            "payload": {"turn_id": "t1", "model": "gpt-6-sol"},
        },
    ]
    for response, tokens in usage:
        rows.append(
            {
                "type": "token_usage_record",
                "timestamp": _stamp(3, 10),
                "payload": {
                    "turn_id": "t1",
                    "response_id": response,
                    "usage": {
                        "input_tokens": tokens - 10,
                        "cached_input_tokens": tokens - 20,
                        "output_tokens": 10,
                    },
                },
            }
        )
    folder = sessions if flat else sessions / "2026" / "09" / "03"
    return _write(folder / f"rollout-2026-09-03T09-00-00-{name}.jsonl", *rows)


@pytest.fixture
def home(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`~` for the collector, with the clock the morning after `DAY` so a day reads as over."""
    monkeypatch.setenv("AGENT_NOW", "2026-09-05T09:00:00-07:00")
    return tmp_path / "home"


def _collect(root: Path, home: Path, day: date = DAY) -> dict[str, Any]:
    record = collect_day(root, day, machine="mac", home=home)
    assert record_problems(record) == []
    return record


def test_dispatch_worktree_transcript_counts_as_its_worker(tmp_root: Path, home: Path):
    worktree = tmp_root.parent / "Agent-worktrees" / "_dispatch" / "alpha" / "001-x-mac-01"
    _dispatch(tmp_root, "001-mac-01", session_id="sess-w")
    _write(_project(home, "any-folder-name") / "sess-w.jsonl", _assistant(worktree, "sess-w", "r1", 1000))
    record = _collect(tmp_root, home)
    assert record["tokens_worker"] == 1000
    assert record["total_tokens"] == 1000  # the dispatch record's `tokens` is never read
    assert record["sources"]["claude"] == {"status": "complete", "files": 1, "requests": 1}


def test_codex_dispatch_rollout_counts_as_its_reviewer(tmp_root: Path, home: Path):
    worktree = tmp_root.parent / "Agent-worktrees" / "_dispatch" / "alpha" / "001-x-mac-02"
    _dispatch(tmp_root, "001-mac-02", provider="codex", row="review", session_id="s", codex_thread_id="th-r")
    _rollout(home / ".codex" / "sessions", "a", "th-r", worktree, ("resp-1", 400))
    _rollout(home / ".codex" / "sessions", "b", "th-child", worktree, ("resp-2", 100), parent="th-r")
    record = _collect(tmp_root, home)
    assert record["tokens_reviewer"] == 500
    assert record["models"]["gpt-6-sol"] == {
        "requests": 2,
        "input": 20,
        "output": 20,
        "cache_write": 0,
        "cache_read": 460,
    }


def test_codex_home_is_honored(tmp_root: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    _rollout(custom / "sessions", "m", "th-m", tmp_root, ("resp-1", 300))
    _rollout(home / ".codex" / "sessions", "ignored", "th-i", tmp_root, ("resp-2", 900))
    record = _collect(tmp_root, home)
    assert record["tokens_manager"] == 300


def test_parent_folder_session_is_left_out_whatever_its_folder(tmp_root: Path, home: Path):
    import re

    folder = _project(home, re.sub(r"[^A-Za-z0-9]", "-", str(tmp_root.resolve())))  # Claude's slug
    _write(folder / "outside.jsonl", _assistant(tmp_root.parent, "sess-o", "r-out", 5000))
    _write(folder / "inside.jsonl", _assistant(tmp_root, "sess-i", "r-in", 100))
    _rollout(home / ".codex" / "sessions", "o", "th-o", tmp_root.parent, ("resp-o", 700))
    record = _collect(tmp_root, home)
    assert record["total_tokens"] == 100
    assert record["tokens_manager"] == 100


def test_request_in_live_and_preserved_copies_counts_once(tmp_root: Path, home: Path):
    row = _assistant(tmp_root, "sess-m", "r-same", 800)
    _write(_project(home, "live") / "sess-m.jsonl", row, row)
    _write(tmp_root / ".agent" / "raw" / "001-x" / "transcripts" / "sess-m.jsonl", row)
    record = _collect(tmp_root, home)
    assert record["total_tokens"] == 800
    assert record["requests"] == 1


def test_a_dispatch_crossing_midnight_splits_across_two_days(tmp_root: Path, home: Path):
    worktree = tmp_root.parent / "Agent-worktrees" / "_dispatch" / "alpha" / "001-x-mac-01"
    _dispatch(tmp_root, "001-mac-01", session_id="sess-w")
    _write(
        _project(home, "w") / "sess-w.jsonl",
        _assistant(worktree, "sess-w", "r-late", 600, hour=23),
        _assistant(worktree, "sess-w", "r-early", 400, day=4, hour=0),
    )
    assert _collect(tmp_root, home)["tokens_worker"] == 600
    assert _collect(tmp_root, home, date(2026, 9, 4))["tokens_worker"] == 400


def test_classes_for_manager_subagent_drafter_worker_reviewer_and_probe(tmp_root: Path, home: Path):
    worktrees = tmp_root.parent / "Agent-worktrees" / "_dispatch" / "alpha"
    _dispatch(tmp_root, "001-mac-01", session_id="sess-w")
    _dispatch(tmp_root, "001-mac-02", session_id="sess-r", row="review")
    _dispatch(tmp_root, "001-mac-03", session_id="sess-p", kind="probe")
    vault = _project(home, "vault")
    _write(vault / "sess-m.jsonl", _assistant(tmp_root, "sess-m", "m1", 1000))
    subagents = vault / "sess-m" / "subagents"
    _write(
        subagents / "agent-a1.jsonl",
        _assistant(tmp_root, "sess-m", "s1", 200, isSidechain=True, attributionAgent="Explore"),
    )
    _write(subagents / "agent-a2.jsonl", _assistant(tmp_root, "sess-m", "d1", 30, isSidechain=True))
    (subagents / "agent-a2.meta.json").write_text(
        json.dumps({"agentType": "journal-drafter"}), encoding="utf-8"
    )
    _write(_project(home, "w") / "sess-w.jsonl", _assistant(worktrees / "w", "sess-w", "w1", 4000))
    _write(
        _project(home, "w") / "sess-w" / "subagents" / "agent-b.jsonl",
        _assistant(worktrees / "w", "sess-w", "w2", 50, isSidechain=True, attributionAgent="journal-drafter"),
    )
    _write(_project(home, "r") / "sess-r.jsonl", _assistant(worktrees / "r", "sess-r", "rv1", 700))
    _write(_project(home, "p") / "sess-p.jsonl", _assistant(worktrees / "p", "sess-p", "p1", 90))
    record = _collect(tmp_root, home)
    assert {
        key: record[f"tokens_{key}"]
        for key in ("manager", "subagent", "drafter", "worker", "reviewer", "probe")
    } == {
        "manager": 1000,
        "subagent": 200,
        "drafter": 30,
        "worker": 4050,
        "reviewer": 700,
        "probe": 90,
    }
    assert record["tokens_other"] == record["tokens_eval"] == 0


def test_codex_child_of_a_manager_is_a_subagent_and_a_review_exec_is_other(
    tmp_root: Path, home: Path, tmp_path: Path
):
    sessions = home / ".codex" / "sessions"
    _rollout(sessions, "m", "th-m", tmp_root, ("resp-m", 300))
    _rollout(sessions, "c", "th-c", tmp_root, ("resp-c", 60), parent="th-m")
    _rollout(sessions, "x", "th-x", tmp_path / "agent-codex-review-a4-XYZ", ("resp-x", 25))
    _rollout(sessions, "e", "th-e", tmp_path / "agent-eval-manager-abc" / "vault", ("resp-e", 40))
    record = _collect(tmp_root, home)
    assert (
        record["tokens_manager"],
        record["tokens_subagent"],
        record["tokens_other"],
        record["tokens_eval"],
    ) == (
        300,
        60,
        25,
        40,
    )


def test_missing_data_folders_are_complete_zero_and_say_absent(tmp_root: Path, home: Path):
    record = _collect(tmp_root, home)
    assert record["status"] == "complete"
    assert record["total_tokens"] == 0
    assert record["dispatches"] == 0
    assert record["sources"]["claude"] == {
        "status": "complete",
        "files": 0,
        "requests": 0,
        "absent": True,
        "path": str(home / ".claude" / "projects"),
    }
    assert record["sources"]["codex"]["absent"] is True
    assert record["sources"]["codex"]["path"] == str(home / ".codex" / "sessions")


def test_an_empty_existing_folder_is_complete_zero_without_absent(tmp_root: Path, home: Path):
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".codex" / "archived_sessions").mkdir(parents=True)
    sources = _collect(tmp_root, home)["sources"]
    assert sources["claude"] == {"status": "complete", "files": 0, "requests": 0}
    assert sources["codex"] == {"status": "complete", "files": 0, "requests": 0}


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
@pytest.mark.parametrize("folder", [(".claude", "projects"), (".codex", "sessions")])
def test_a_folder_that_exists_but_cannot_be_read_is_unavailable(
    tmp_root: Path, home: Path, folder: tuple[str, str]
):
    locked = home.joinpath(*folder)
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        record = _collect(tmp_root, home)
    finally:
        locked.chmod(0o755)
    source = record["sources"]["claude" if folder[0] == ".claude" else "codex"]
    assert source["status"] == "unavailable" and "absent" not in source
    assert record["status"] == "partial"


def test_today_stays_partial_until_a_collection_after_midnight(
    tmp_root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write(_project(home, "vault") / "sess-m.jsonl", _assistant(tmp_root, "sess-m", "m1", 100))
    monkeypatch.setenv("AGENT_NOW", datetime(2026, 9, 3, 23, 59).astimezone().isoformat())
    record = _collect(tmp_root, home)
    assert record["status"] == "partial"
    assert {s["status"] for s in record["sources"].values()} == {"partial"}
    assert record["sources"]["claude"]["reason"] == "collected before the day ended"
    assert record["total_tokens"] == 100
    monkeypatch.setenv("AGENT_NOW", datetime(2026, 9, 4, 0, 1).astimezone().isoformat())
    assert _collect(tmp_root, home)["status"] == "complete"


def test_codex_journal_drafter_spawn_is_a_drafter_not_a_subagent(tmp_root: Path, home: Path):
    sessions = home / ".codex" / "sessions"
    _rollout(sessions, "m", "th-m", tmp_root, ("resp-m", 300))
    _rollout(sessions, "d", "th-d", tmp_root, ("resp-d", 70), spawn=("th-m", "/root/journal_drafter"))
    _rollout(sessions, "s", "th-s", tmp_root, ("resp-s", 40), spawn=("th-m", "/root/retro_055"))
    record = _collect(tmp_root, home)
    assert (record["tokens_manager"], record["tokens_drafter"], record["tokens_subagent"]) == (300, 70, 40)


def test_archived_rollouts_count_once_beside_live_ones(tmp_root: Path, home: Path):
    codex = home / ".codex"
    _rollout(codex / "sessions", "live", "th-l", tmp_root, ("resp-l", 300))
    _rollout(codex / "archived_sessions", "old", "th-o", tmp_root, ("resp-o", 200), flat=True)
    # The same response archived under another rollout name is counted once.
    _rollout(codex / "archived_sessions", "copy", "th-c", tmp_root, ("resp-l", 300), flat=True)
    # The same file name in both folders is read once, from `sessions`.
    _rollout(codex / "archived_sessions", "live", "th-l", tmp_root, ("resp-l", 300), flat=True)
    record = _collect(tmp_root, home)
    assert record["tokens_manager"] == 500
    assert record["sources"]["codex"]["requests"] == 2
    assert record["sources"]["codex"]["files"] == 2
