"""An existing notes folder does not have to be a Git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_cli import notes
from agent_cli.doctor import checks_notes


def test_doctor_accepts_a_linked_notes_folder_without_git(tmp_root: Path):
    folder = tmp_root.parent / "private-notes"
    folder.mkdir()
    notes.write_notes_root(tmp_root, folder)
    row = checks_notes.check_notes_repo(tmp_root)[0]
    assert row["level"] == "ok"
    assert row["detail"] == str(folder)


def test_doctor_warns_when_the_notes_repo_has_no_remote(tmp_root: Path, notes_remote: Path):
    assert checks_notes.check_notes_repo(tmp_root)[0]["level"] == "ok"
    subprocess.run(["git", "remote", "remove", "origin"], cwd=notes_remote, check=True)
    row = checks_notes.check_notes_repo(tmp_root)[0]
    assert row["level"] == "warn"
    assert "second device" in row["detail"] and "private remote" in row["detail"]


def _standing(monkeypatch, failure):
    from agent_cli import notes_sync

    monkeypatch.setattr(notes_sync, "standing_failure", lambda root: failure)


def test_a_standing_sync_failure_warns_and_a_halt_fails(tmp_root: Path, monkeypatch):
    (tmp_root / "agent.local.yaml").write_text("notes_sync: pull\n", encoding="utf-8")
    _standing(monkeypatch, {"id": "x", "status": "offline", "detail": "fetch failed"})
    row = checks_notes.check_notes_sync(tmp_root)[0]
    assert (row["level"], row["detail"]) == ("warn", "offline: fetch failed")
    halt = "rebase onto upstream failed; ... run `agent notes-sync clear`"
    _standing(monkeypatch, {"id": "h", "status": "halted", "detail": halt})
    row = checks_notes.check_notes_sync(tmp_root)[0]
    assert row["level"] == "fail" and "agent notes-sync clear" in row["detail"]


def test_the_sync_row_is_silent_with_nothing_standing_or_the_switch_off(tmp_root: Path, monkeypatch):
    (tmp_root / "agent.local.yaml").write_text("notes_sync: pull\n", encoding="utf-8")
    _standing(monkeypatch, None)
    assert checks_notes.check_notes_sync(tmp_root) == []
    (tmp_root / "agent.local.yaml").write_text('notes_sync: "off"\n', encoding="utf-8")
    _standing(monkeypatch, {"id": "x", "status": "offline", "detail": "fetch failed"})
    assert checks_notes.check_notes_sync(tmp_root) == []
