"""A bug: a Codex tab closed, then reopened with `codex resume`, kept no label and
no status. The owner deferred it on 2026-10-03, "defer, no guessing": with no marker linking the
thread to a tab, nothing is claimed, and these tests hold that line."""

from __future__ import annotations

from pathlib import Path

from agent_cli import obsidian_tabs, sessions, tab_drain
from agent_cli.hooks.activity import codex_activity

SID = "019b76da-a800-7000-8000-000000000002"


def _claims(monkeypatch) -> list[dict]:
    claims: list[dict] = []

    def claim(name: str, label: str, **kwargs) -> bool:
        claims.append({"name": name, "label": label, **kwargs})
        return True

    monkeypatch.setattr(obsidian_tabs, "claim_active", claim)
    monkeypatch.setattr("agent_cli.codex_tab_link.marker_for", lambda s: "")  # an old thread: no marker
    monkeypatch.setattr(tab_drain, "request", lambda *args, **kw: {"started": True})
    return claims


def test_a_resume_with_no_marker_and_no_old_display_claims_nothing(tmp_root: Path, monkeypatch):
    """The active leaf may be an untitled terminal and no other Codex session may be registered,
    and the claim still does not take it: an untitled leaf is not proof that the tab is this
    thread's."""
    sessions.set_record(
        tmp_root, SID, feature={"id": 1, "slug": "example"}, name="mgr-001-example-codex-00000002"
    )
    sessions.set_field(tmp_root, SID, "provider", "codex")
    assert [r for r in sessions.all_records(tmp_root) if r.get("session_id") != SID] == []
    claims = _claims(monkeypatch)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionStart", "source": "resume"})
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    assert claims == []


def test_a_new_codex_thread_with_no_marker_still_claims_nothing(tmp_root: Path, monkeypatch):
    """#67 stands: no marker, no claim."""
    sessions.set_record(tmp_root, SID, name="mgr-codex-00000002")
    claims = _claims(monkeypatch)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionStart"})
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    assert claims == []
