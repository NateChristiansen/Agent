"""Publishing this machine's usage records to the notes repo: plan, collect, install, per mode.

Real git: `notes_remote` gives a notes clone tracking a bare remote, linked as the notes root.
The collector reads a fake home, so the owner's own transcripts are never read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_cli import frontmatter, links, notes
from agent_cli.usage import collect_day, files, publish
from agent_cli.usage.schema import checkpoint_problems, record_problems
from usage_helpers import sample_checkpoint, sample_record

TODAY = date(2026, 9, 3)
TODAY_REL = "usage/2026/09-September/03-Thursday.mac.md"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _mode(root: Path, mode: str) -> None:
    (root / "agent.local.yaml").write_text(f"machine: mac\nnotes_sync: {mode}\n", encoding="utf-8")


@pytest.fixture
def clone(tmp_root: Path, notes_remote: Path) -> Path:
    _mode(tmp_root, "on")
    return notes_remote


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _gather(home: Path, seen: list[date] | None = None) -> Any:
    def gather(root: Path, day: date, tag: str) -> dict[str, Any]:
        if seen is not None:
            seen.append(day)
        return collect_day(root, day, machine=tag, home=home)

    return gather


def _transcript(home: Path, root: Path, request: str, tokens: int, hour: int = 10) -> None:
    stamp = datetime(2026, 9, 3, hour).astimezone().isoformat()
    row = {
        "type": "assistant",
        "cwd": str(root),
        "sessionId": "sess-m",
        "requestId": request,
        "timestamp": stamp,
        "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": tokens, "output_tokens": 0}},
    }
    path = home / ".claude" / "projects" / "vault" / "sess-m.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _remote_meta(clone: Path, rel: str) -> dict[str, Any]:
    git(clone, "fetch", "-q", "origin")
    meta, _ = frontmatter.split(git(clone, "show", f"origin/main:{rel}") + "\n")
    return meta


def _write_meta(where: Path, rel: str, meta: dict[str, Any]) -> None:
    path = where / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(files.render(meta), encoding="utf-8")


def test_todays_record_is_published_and_partial(tmp_root: Path, clone: Path, home: Path):
    _transcript(home, tmp_root, "r1", 700)
    outcome = publish.publish(tmp_root, collect=_gather(home))
    assert outcome.status == "pushed", outcome
    record = _remote_meta(clone, TODAY_REL)
    assert record_problems(record) == []
    assert record["status"] == "partial"
    assert record["sources"]["claude"]["reason"] == "collected before the day ended"
    assert record["total_tokens"] == record["tokens_manager"] == 700
    mark = _remote_meta(clone, "usage/machines/mac.md")
    assert checkpoint_problems(mark) == []
    assert mark["sources"]["claude"]["status"] == "complete"
    # Days with no activity get no file; the checkpoint says they were covered.
    assert git(clone, "ls-tree", "-r", "--name-only", "origin/main", "usage") == "\n".join(
        sorted([TODAY_REL, "usage/machines/mac.md", "usage/telemetry/2026-09-03.mac.json"])
    )


def test_cadence_refreshes_age_even_when_figures_are_unchanged(
    tmp_root: Path, clone: Path, home: Path, monkeypatch
):
    """Publication refreshes observation times at the cadence boundary, so ages stay honest."""
    _transcript(home, tmp_root, "r1", 700)
    assert publish.publish(tmp_root, collect=_gather(home)).status == "pushed"
    head = git(clone, "rev-parse", "origin/main")
    from agent_cli import core

    monkeypatch.setenv("AGENT_NOW", core.iso(core.now() + timedelta(seconds=1801)))
    again = publish.publish(tmp_root, collect=_gather(home))
    assert again.status == "pushed" and TODAY_REL in again.written
    git(clone, "fetch", "-q", "origin")
    assert git(clone, "rev-parse", "origin/main") != head
    _transcript(home, tmp_root, "r2", 300)
    assert publish.publish(tmp_root, collect=_gather(home)).status == "pushed"
    assert _remote_meta(clone, TODAY_REL)["total_tokens"] == 1000


def test_a_stale_earlier_day_is_collected_again(tmp_root: Path, clone: Path, home: Path):
    stale = sample_record(date="2026-08-20", status="partial")
    stale["sources"]["claude"] = {
        "status": "partial",
        "files": 2,
        "requests": 2,
        "reason": "1 file unreadable",
    }
    _write_meta(clone, "usage/2026/08-August/20-Thursday.mac.md", stale)
    covered = datetime(2026, 9, 2).astimezone().isoformat()
    mark = sample_checkpoint(covered_through=covered)
    behind = datetime(2026, 9, 1).astimezone().isoformat()
    mark["sources"]["codex"]["covered_through"] = mark["covered_through"] = behind
    _write_meta(clone, "usage/machines/mac.md", mark)
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "earlier usage")
    seen: list[date] = []
    outcome = publish.publish(tmp_root, collect=_gather(home, seen))
    assert outcome.status == "pushed", outcome
    # The partial day, and every day from the earliest source mark (codex) on.
    assert seen == [date(2026, 8, 20), date(2026, 9, 1), date(2026, 9, 2), TODAY]
    assert _remote_meta(clone, "usage/2026/08-August/20-Thursday.mac.md")["status"] == "complete"


def test_a_budget_overrun_leaves_the_rest_pending(tmp_root: Path, clone: Path, home: Path):
    ticks = iter(range(1000))
    seen: list[date] = []
    first = publish.publish(tmp_root, budget=2.5, clock=lambda: next(ticks), collect=_gather(home, seen))
    assert first.status == "pushed"
    assert seen == [date(2026, 8, 4), date(2026, 8, 5)]
    assert first.left[0] == "2026-08-06" and first.left[-1] == "2026-09-03"
    assert first.progress
    mark = _remote_meta(clone, "usage/machines/mac.md")
    assert str(mark["covered_through"]) == datetime(2026, 8, 6).astimezone().isoformat()
    second = publish.publish(tmp_root, collect=_gather(home))
    assert second.collected[0] == "2026-08-06" and second.left == []


def test_a_partial_source_does_not_hide_the_progress_of_the_others(tmp_root: Path, clone: Path, home: Path):
    def partial_claude(root: Path, day: date, tag: str) -> dict[str, Any]:
        record = collect_day(root, day, machine=tag, home=home)
        record["sources"]["claude"].update(status="partial", reason="1 file(s) unreadable")
        record["status"] = "partial"
        return record

    ticks = iter(range(1000))
    first = publish.publish(tmp_root, budget=2.5, clock=lambda: next(ticks), collect=partial_claude)
    stuck = datetime(2026, 8, 4).astimezone().isoformat()
    assert first.progress, "codex and costs covered two more days"
    assert first.pending == ["29 day(s) not reached, from 2026-08-06", f"claude partial at {stuck}"]
    seen: list[date] = []
    ticks = iter(range(1000))
    publish.publish(tmp_root, budget=3.5, clock=lambda: next(ticks), collect=_gather(home, seen))
    # The stuck source is retried at its own day; the others carry on where they stopped.
    assert seen == [date(2026, 8, 4), date(2026, 8, 6), date(2026, 8, 7)]


def test_off_mode_still_writes_under_the_notes_repo_lock(
    tmp_root: Path, clone: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli import notes_sync
    from agent_cli.usage import install

    _mode(tmp_root, "off")
    monkeypatch.setattr(install, "LOCK_WAIT_S", 0.2)
    with notes_sync.repo_lock(clone / ".git", 1):
        result = install.install(tmp_root, {"usage/machines/mac.md": "x\n"}, "s", "b")
    assert result.status == "locked"
    assert not (clone / "usage" / "machines" / "mac.md").exists()
    assert install.install(tmp_root, {"usage/machines/mac.md": "x\n"}, "s", "b").status == "written"


def test_a_linked_git_repo_git_cannot_open_gets_no_write(
    tmp_root: Path, clone: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli import notes_sync
    from agent_cli.usage import install

    monkeypatch.setattr(notes_sync, "open_repo", lambda root, budget: notes_sync.Result("none"))
    result = install.install(tmp_root, {"usage/machines/mac.md": "x\n"}, "s", "b")
    assert result.status == "error", result
    assert not (clone / "usage" / "machines" / "mac.md").exists()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_changed_unavailable_evidence_is_not_settled(tmp_root: Path, clone: Path, home: Path):
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)
    projects.chmod(0)
    try:
        first = publish.publish(tmp_root, collect=_gather(home))
    finally:
        projects.chmod(0o755)
    stuck = [f"claude unavailable at {datetime(2026, 8, 4).astimezone().isoformat()}"]
    assert first.pending == stuck
    # Same status, same mark, but another reason: permission denied, then not a directory.
    projects.rmdir()
    projects.write_text("not a folder\n", encoding="utf-8")
    second = publish.publish(tmp_root, collect=_gather(home))
    assert (second.pending, second.settled) == (stuck, [])
    third = publish.publish(tmp_root, collect=_gather(home))
    assert (third.pending, third.settled) == ([], stuck)


def test_only_this_machines_files_are_written(tmp_root: Path, clone: Path, home: Path, monkeypatch):
    theirs = {
        "usage/2026/09-September/03-Thursday.win.md": "theirs\n",
        "usage/machines/win.md": "theirs too\n",
    }
    for rel, text in theirs.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(text, encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "the other machine")
    opened: list[Path] = []
    real = files.read_meta
    monkeypatch.setattr(files, "read_meta", lambda path: opened.append(path) or real(path))
    _transcript(home, tmp_root, "r1", 50)
    assert publish.publish(tmp_root, collect=_gather(home)).status == "pushed"
    for rel, text in theirs.items():
        assert (clone / rel).read_text(encoding="utf-8") == text
    assert not [p for p in opened if ".win" in p.name or p.name == "win.md"]
    changed = git(clone, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(changed) == sorted(
        [TODAY_REL, "usage/machines/mac.md", "usage/telemetry/2026-09-03.mac.json"]
    )


@pytest.mark.parametrize(("mode", "status"), [("off", "written"), ("pull", "committed"), ("on", "pushed")])
def test_each_notes_sync_mode(tmp_root: Path, clone: Path, home: Path, mode: str, status: str):
    _mode(tmp_root, mode)
    head, remote = git(clone, "rev-parse", "HEAD"), git(clone, "rev-parse", "origin/main")
    _transcript(home, tmp_root, "r1", 50)
    assert publish.publish(tmp_root, collect=_gather(home)).status == status
    assert (clone / TODAY_REL).is_file()
    git(clone, "fetch", "-q", "origin")
    assert (git(clone, "rev-parse", "HEAD") != head) == (mode != "off")
    assert (git(clone, "rev-parse", "origin/main") != remote) == (mode == "on")


def test_a_managers_staged_note_is_not_swept_into_the_usage_commit(tmp_root: Path, clone: Path, home: Path):
    _mode(tmp_root, "pull")
    (clone / "draft.md").write_text("half written\n", encoding="utf-8")
    git(clone, "add", "draft.md")
    assert publish.publish(tmp_root, collect=_gather(home)).status == "committed"
    assert "draft.md" not in git(clone, "show", "--name-only", "--format=", "HEAD")
    assert git(clone, "diff", "--cached", "--name-only") == "draft.md"


def test_usage_is_private_and_linked_like_the_journal(tmp_root: Path, tmp_path: Path):
    assert any(e.path == "usage" and e.kind == "dir" for e in notes.PRIVATE_SET)
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/usage" in ignore and "/journal" in ignore
    notes_repo = tmp_path / "linked-notes"
    notes_repo.mkdir()
    rows = {row["path"]: row["state"] for row in notes.link(tmp_root, notes_repo)}
    assert rows["usage"] == "done"
    assert links.is_link(tmp_root / "usage") and (notes_repo / "usage").is_dir()
    assert {r["path"]: r["state"] for r in notes.status(tmp_root)}["usage"] == "linked"
