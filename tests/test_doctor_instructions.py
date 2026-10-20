"""Doctor's `project instructions` check: AGENTS.md loads and no CLAUDE.md suppresses it.

Split from test_doctor.py at the 500-line gate.
"""

from __future__ import annotations

from pathlib import Path


def test_project_instructions_check(tmp_root: Path, run):
    """ADR-0024 deleted CLAUDE.md so AGENTS.md is the one always-loaded file on both clients.

    A CLAUDE.md anywhere at or above the working directory makes Claude Code read CLAUDE.md
    files only and skip every AGENTS.md — no error, nothing in the transcript. Prose cannot hold
    that; this check is the boundary that keeps it deleted, including in a parent directory.
    """
    run(["status", "render"])
    (tmp_root / "AGENTS.md").write_text("# how this repo works\n")
    r = run(["--json", "doctor"])
    row = [c for c in r.json if c["check"] == "project instructions"][-1]
    assert row["level"] == "ok" and "no CLAUDE.md" in row["detail"]

    (tmp_root / "CLAUDE.md").write_text("@AGENTS.md\n")
    r = run(["--json", "doctor"])
    row = [c for c in r.json if c["check"] == "project instructions"][-1]
    assert row["level"] == "fail"
    assert "suppresses AGENTS.md" in row["detail"] and "CLAUDE.md" in row["detail"]
    (tmp_root / "CLAUDE.md").unlink()

    # a parent directory's file costs just as much, and is the case nobody thinks to check
    (tmp_root.parent / "CLAUDE.local.md").write_text("notes\n")
    r = run(["--json", "doctor"])
    row = [c for c in r.json if c["check"] == "project instructions"][-1]
    assert row["level"] == "fail" and "CLAUDE.local.md" in row["detail"]
    (tmp_root.parent / "CLAUDE.local.md").unlink()

    (tmp_root / "AGENTS.md").unlink()
    r = run(["--json", "doctor"])
    row = [c for c in r.json if c["check"] == "project instructions"][-1]
    assert row["level"] == "fail" and "AGENTS.md is missing" in row["detail"]


def test_a_nested_claude_md_in_any_case_is_flagged(tmp_root: Path, run):
    """052: a scratch report named `claude.md` loaded as instructions on a case-insensitive
    file system. A nested one is a warning, not the suppression failure above."""
    run(["status", "render"])
    (tmp_root / "AGENTS.md").write_text("# how this repo works\n")
    row = [c for c in run(["--json", "doctor"]).json if c["check"] == "nested instructions"][-1]
    assert row["level"] == "ok"

    scratch = tmp_root / ".agent" / "scratch" / "audit"
    scratch.mkdir(parents=True)
    (scratch / "claude.md").write_text("# a report\n")
    row = [c for c in run(["--json", "doctor"]).json if c["check"] == "nested instructions"][-1]
    assert row["level"] == "warn" and "claude.md" in row["detail"]

    # every vault directory counts, including capture and dispatch state (review, 052)
    (scratch / "claude.md").unlink()
    raw = tmp_root / ".agent" / "raw" / "052-x"
    raw.mkdir(parents=True)
    (raw / "CLAUDE.md").write_text("# x\n")
    row = [c for c in run(["--json", "doctor"]).json if c["check"] == "nested instructions"][-1]
    assert row["level"] == "warn" and ".agent/raw/052-x/CLAUDE.md" in row["detail"]
