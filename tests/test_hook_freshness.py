"""Live managers whose hook wiring predates their client's hook config are marked.

A live check run in a Codex session started before the wiring
change failed: a running client keeps the wiring it loaded at start.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agent_cli import hook_freshness, ops, sessions

CHANGED = datetime.fromisoformat("2026-09-24T20:00:00-06:00")


def _changed(root: Path, provider: str):
    return CHANGED if provider in ("claude", "codex") else None


def test_a_session_started_before_the_change_is_stale_and_one_after_is_not(monkeypatch, tmp_root: Path):
    monkeypatch.setattr(hook_freshness, "changed_at", _changed)
    stale = {"provider": "codex", "started_at": "2026-09-24T17:56:00-06:00"}
    fresh = {"provider": "claude", "started_at": "2026-09-24T20:05:00-06:00"}
    assert hook_freshness.stale_since(tmp_root, stale, {}) == CHANGED
    assert hook_freshness.stale_since(tmp_root, fresh, {}) is None
    assert ".codex/hooks.json" in hook_freshness.mark(tmp_root, stale, {})
    assert hook_freshness.mark(tmp_root, fresh, {}) == ""


def test_a_session_with_no_start_or_no_change_is_not_marked(monkeypatch, tmp_root: Path):
    monkeypatch.setattr(hook_freshness, "changed_at", lambda root, provider: None)
    assert hook_freshness.stale_since(tmp_root, {"started_at": "2026-09-24T17:56:00-06:00"}, {}) is None
    monkeypatch.setattr(hook_freshness, "changed_at", _changed)
    assert hook_freshness.stale_since(tmp_root, {"provider": "claude"}, {}) is None


def test_changed_at_reads_the_last_commit_touching_the_config(tmp_root: Path):
    # tmp_root is a git repo; with no commit touching .codex/hooks.json there is nothing to compare.
    assert hook_freshness.changed_at(tmp_root, "codex") is None
    assert hook_freshness.changed_at(tmp_root, "unknown") is None


def test_peer_list_shows_the_hooks_column(monkeypatch, tmp_root: Path):
    monkeypatch.setattr(hook_freshness, "changed_at", _changed)
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    sessions.set_record(tmp_root, "old", name="mgr-old", pid=1)
    sessions.set_field(tmp_root, "old", "started_at", "2026-09-24T17:56:00-06:00")
    sessions.set_record(tmp_root, "new", name="mgr-new", pid=1)
    sessions.set_field(tmp_root, "new", "started_at", "2026-09-24T20:05:00-06:00")
    text = ops.peer_list(tmp_root).text
    row = {line.split()[0]: line.split()[-1] for line in text.splitlines() if line.startswith("mgr-")}
    assert row == {"mgr-old": "stale", "mgr-new": "ok"}
