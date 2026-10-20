"""Regression tests for the 2026-09-03 adversarial review findings."""

import json
import sys
from pathlib import Path

import pytest

from agent_cli import capture, frontmatter
from agent_cli import log as log_mod
from agent_cli.core import AgentError
from agent_cli.features import add_task_at, list_tasks, resolve

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_feature_slug_cannot_look_like_a_ref(tmp_root: Path, run):
    for slug in ("7", "001", "current", "new", "list"):
        r = run(["feature", "new", slug, "--title", "t"])
        assert r.code == 2, slug
        assert "collide" in r.err
    assert not (tmp_root / "features").exists() or not any((tmp_root / "features").iterdir())
    # ADR slugs are not feature refs. "new" is fine there.
    assert run(["adr", "new", "new", "--title", "New", "--scope", "harness"]).code == 0


def test_feature_ids_past_999(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    (tmp_root / "features/001-x").rename(tmp_root / "features/999-x")
    r = run(["--json", "feature", "new", "y", "--title", "Y"])
    assert r.json["dir"] == "features/1000-y"
    assert [f["id"] for f in run(["--json", "feature", "list"]).json] == [999, 1000]
    assert run(["--json", "feature", "show", "1000"]).json["slug"] == "y"


def test_task_parser_variants():
    body = (
        "## Tasks (phase 2)\n"
        "- [ ] dash open\n"
        "* [x] star done\n"
        "+ [ ] plus open\n"
        "- [/] in progress counts as open\n"
        "- [-] superseded is neither open nor done\n"
        "  - [ ] nested is not a task\n"
        "- [ ]\n"
        "\n## Other\n- [ ] outside the section\n"
    )
    tasks = list_tasks(body)
    assert [(t.index, t.done, t.text) for t in tasks] == [
        (1, False, "dash open"),
        (2, True, "star done"),
        (3, False, "plus open"),
        (4, False, "in progress counts as open"),
        (5, False, "superseded is neither open nor done"),
    ]
    # `[-]` is superseded, as Dataview counts it (`completed` only for `x`)
    assert tasks[4].superseded and not tasks[4].is_open
    with pytest.raises(AgentError, match="one line"):
        add_task_at(body, "two\nlines")


def test_frontmatter_empty_block_and_roundtrip(tmp_path: Path):
    meta, body = frontmatter.split("---\n---\n\n# hi\n")
    assert meta == {} and body == "# hi\n"
    meta, body = frontmatter.split("---\n---")
    assert meta == {} and body == ""


def test_adopt_moves_only_that_session(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    for sid in ("s1", "s2", "s1"):
        run(
            ["capture", "--kind", "prompt", "--stdin"],
            stdin=json.dumps({"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": sid}),
        )
    unassigned = tmp_root / ".agent/raw/unassigned/2026-09-03.jsonl"
    assert len(unassigned.read_text().splitlines()) == 3
    feature = resolve(tmp_root, "1")
    assert capture.adopt(tmp_root, feature, "s1")["moved"] == 2
    moved = [
        json.loads(line) for line in (tmp_root / ".agent/raw/001-x/2026-09-03.jsonl").read_text().splitlines()
    ]
    assert [m["session_id"] for m in moved] == ["s1", "s1"]
    assert all(m["feature"] == 1 for m in moved)
    left = [json.loads(line) for line in unassigned.read_text().splitlines()]
    assert [m["session_id"] for m in left] == ["s2"]
    assert capture.adopt(tmp_root, feature, "s2")["moved"] == 1
    assert not unassigned.exists()


def test_feature_new_with_use_adopts_only_the_calling_session(tmp_root: Path, run):
    for sid in ("founder", "other", "founder"):
        run(
            ["capture", "--kind", "prompt", "--stdin"],
            stdin=json.dumps({"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": sid}),
        )
    result = run(["--json", "feature", "new", "x", "--title", "X", "--use", "--session-id", "founder"])
    assert result.json["adopted"] == 2
    moved = [
        json.loads(line) for line in (tmp_root / ".agent/raw/001-x/2026-09-03.jsonl").read_text().splitlines()
    ]
    assert [record["session_id"] for record in moved] == ["founder", "founder"]
    assert all(record["feature"] == 1 for record in moved)
    left = [
        json.loads(line)
        for line in (tmp_root / ".agent/raw/unassigned/2026-09-03.jsonl").read_text().splitlines()
    ]
    assert [record["session_id"] for record in left] == ["other"]


def test_adopt_only_rewrites_files_it_moved_from(tmp_root: Path, run):
    """`moved` used to be a single counter shared across every unassigned file. A later file
    with nothing of this session's in it still got rewritten, and its blank lines silently
    dropped, once an earlier file had moved something. Each file's rewrite must depend only
    on what moved out of THAT file."""
    run(["feature", "new", "x", "--title", "X"])
    unassigned = tmp_root / ".agent/raw/unassigned"
    unassigned.mkdir(parents=True, exist_ok=True)
    day1 = unassigned / "2026-09-01.jsonl"
    day2 = unassigned / "2026-09-02.jsonl"
    day1.write_text(json.dumps({"session_id": "s1", "kind": "prompt"}) + "\n", encoding="utf-8")
    day2_text = json.dumps({"session_id": "s2", "kind": "prompt"}) + "\n\n"  # trailing blank line
    day2.write_text(day2_text, encoding="utf-8")
    before_mtime = day2.stat().st_mtime_ns
    feature = resolve(tmp_root, "1")
    result = capture.adopt(tmp_root, feature, "s1")
    assert result["moved"] == 1
    assert result["files"] == ["2026-09-01.jsonl"]  # day2 never touched: nothing of s1's in it
    assert day2.stat().st_mtime_ns == before_mtime
    assert day2.read_text(encoding="utf-8") == day2_text  # untouched, blank line and all
    assert not day1.exists()  # day1's only record moved, so the file is gone


def test_raw_preserve_copies_referenced_transcripts(tmp_root: Path, run):
    """`raw preserve` is `feature_close`'s transcript copy (plan 3.5) exposed standalone, for
    a feature still open when Claude Code's 30-day cleanup is getting close."""
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    transcript = tmp_root.parent / "t.jsonl"
    transcript.write_text("hi\n", encoding="utf-8")
    run(
        ["capture", "--kind", "stop", "--stdin"],
        stdin=json.dumps({"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(transcript)}),
    )
    r = run(["--json", "raw", "preserve", "1"])
    assert r.code == 0, r.err
    assert r.json["copied"] == [".agent/raw/001-x/transcripts/t.jsonl"]
    assert (tmp_root / ".agent/raw/001-x/transcripts/t.jsonl").exists()
    r = run(["raw", "preserve", "1"])
    assert "preserved 0 transcript(s)" in r.out, "already-copied transcript is not re-copied"


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows has no POSIX owner-only file mode; chmod sets only read-only"
)
def test_raw_capture_is_owner_only(tmp_root: Path, run):
    run(
        ["capture", "--kind", "prompt", "--stdin"],
        stdin=json.dumps({"session_id": "s", "hook_event_name": "UserPromptSubmit"}),
    )
    mode = (tmp_root / ".agent/raw/unassigned/2026-09-03.jsonl").stat().st_mode & 0o777
    assert mode == 0o600


def test_unexpected_exception_is_not_a_traceback(tmp_root: Path, run, monkeypatch):
    from agent_cli import cli

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(cli.HANDLERS, ("status", "check"), boom)
    r = run(["--json", "status", "check"])
    assert r.code == 1
    assert r.json == {"error": "RuntimeError: kaboom", "code": 1}
    assert "Traceback" not in r.err


def test_write_survives_render_failure(tmp_root: Path, run):
    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "new", "b", "--title", "B"])
    (tmp_root / "features/002-b/plan.md").write_text("no frontmatter here\n")
    r = run(["log", "1", "still recorded"])
    assert r.code == 0
    assert "re-render failed" in r.err
    assert "still recorded" in (tmp_root / "features/001-a/log.md").read_text()
    # and a retry does not duplicate anything
    entries = log_mod.parse(tmp_root / "features/001-a/log.md")
    assert len(entries) == 1
