from pathlib import Path

import pytest

from agent_cli import features, frontmatter
from agent_cli.core import AgentError

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_new_allocates_sequential_ids(tmp_root: Path, run):
    r1 = run(["--json", "feature", "new", "First Thing", "--title", "First"])
    r2 = run(["--json", "feature", "new", "second-thing", "--title", "Second"])
    assert r1.code == 0 and r2.code == 0
    assert r1.json["dir"] == "features/001-first-thing"
    assert r2.json["dir"] == "features/002-second-thing"
    assert (tmp_root / "features/001-first-thing/plan.md").exists()
    assert (tmp_root / "features/001-first-thing/log.md").read_text(
        encoding="utf-8"
    ) == "# Log — 001 first-thing\n"


def test_current_does_not_read_a_stale_default_feature_file(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    (tmp_root / ".agent/default-feature").write_text("1\n")

    with pytest.raises(AgentError, match="name a feature explicitly"):
        features.resolve(tmp_root, "current")


def test_explicit_closed_feature_ref_remains_available(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    assert run(["feature", "close", "1"]).code == 0

    shown = run(["--json", "feature", "show", "1"])
    assert shown.code == 0
    assert shown.json["status"] == "done"


def test_new_frontmatter_and_body(tmp_root: Path, run):
    run(
        [
            "feature",
            "new",
            "thing",
            "--title",
            "A thing",
            "--ticket",
            "TK#123",
            "JIRA-456",
            "--repo",
            "alpha",
            "--tag",
            "x",
        ]
    )
    meta, body = frontmatter.load(tmp_root / "features/001-thing/plan.md")
    assert set(meta) == {
        "id",
        "slug",
        "title",
        "status",
        "opened",
        "closed",
        "tickets",
        "repos",
        "adrs",
        "decisions",
        "tags",
        "links",
    }
    assert meta["id"] == 1
    assert meta["slug"] == "thing"
    assert meta["status"] == "active"
    assert str(meta["opened"]) == "2026-09-03"
    assert meta["closed"] is None
    assert meta["tickets"] == ["TK#123", "JIRA-456"]
    assert meta["repos"] == ["alpha-api"]
    assert meta["tags"] == ["feature", "x"]
    assert meta["links"] == []
    assert body.startswith("# 001 — A thing\n")
    assert "## Open questions" in body


def test_list_flags_accept_repeats_and_lists(tmp_root: Path, run):
    r = run(
        [
            "--json",
            "feature",
            "new",
            "x",
            "--title",
            "X",
            "--repo",
            "alpha",
            "--repo",
            "beta",
            "--ticket",
            "TK#1",
            "TK#2",
            "--ticket",
            "#3",
        ]
    )
    assert r.json["repos"] == ["alpha-api", "beta-web"]
    assert r.json["tickets"] == ["TK#1", "TK#2", "#3"]
    r = run(["--json", "feature", "set", "1", "--add-tag", "a", "--add-tag", "b", "c"])
    assert r.json["tags"] == ["feature", "a", "b", "c"]


def test_bad_slug_and_unknown_repo(tmp_root: Path, run):
    assert run(["feature", "new", "!!!", "--title", "t"]).code == 2
    r = run(["feature", "new", "ok", "--title", "t", "--repo", "nope"])
    assert r.code == 2
    assert "unknown repo" in r.err


def test_duplicate_slug_rejected(tmp_root: Path, run):
    assert run(["feature", "new", "dup", "--title", "t"]).code == 0
    assert run(["feature", "new", "dup", "--title", "t"]).code == 2


def test_list_filters(tmp_root: Path, run):
    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "new", "b", "--title", "B", "--status", "proposed"])
    run(["feature", "close", "1"])
    assert [f["slug"] for f in run(["--json", "feature", "list"]).json] == ["b"]
    assert [f["slug"] for f in run(["--json", "feature", "list", "--all"]).json] == ["a", "b"]
    assert [f["slug"] for f in run(["--json", "feature", "list", "--status", "done"]).json] == ["a"]


def test_show_and_ref_resolution(tmp_root: Path, run):
    run(["feature", "new", "my-feature", "--title", "Mine"])
    for ref in ("1", "001", "001-my-feature", "my-feature"):
        r = run(["--json", "feature", "show", ref])
        assert r.code == 0, ref
        assert r.json["id"] == 1
    assert run(["feature", "show", "9"]).code == 1
    assert run(["feature", "show", "nope"]).code == 1


def test_close_sets_date_and_rejects_double_close(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["--json", "feature", "close", "1", "--note", "shipped"])
    assert r.code == 0
    assert r.json["status"] == "done"
    assert r.json["closed"] == "2026-09-03"
    assert "> [!decision] 2026-09-03 14:05\n> shipped" in (tmp_root / "features/001-x/log.md").read_text(
        encoding="utf-8"
    )
    assert run(["feature", "close", "1"]).code == 2


def test_set_fields(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(
        [
            "--json",
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--add-ticket",
            "TK#9",
            "--add-repo",
            "beta",
            "--title",
            "Y",
        ]
    )
    assert r.code == 0
    assert r.json["status"] == "blocked"
    assert r.json["tickets"] == ["TK#9"]
    assert r.json["repos"] == ["beta-web"]
    assert r.json["title"] == "Y"
    assert run(["feature", "set", "1", "--add-repo", "bogus"]).code == 2
    r = run(["--json", "feature", "set", "1", "--remove-ticket", "TK#9"])
    assert r.json["tickets"] == []
    assert run(["feature", "set", "1", "--add-ticket", "has space"]).code == 2


def test_tasks_parsed_from_body(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    plan = tmp_root / "features/001-x/plan.md"
    meta, body = frontmatter.load(plan)
    body = body.replace("## Tasks\n- [ ]\n", "## Tasks\n- [x] done one\n- [ ] do two\n- [ ] do three\n")
    frontmatter.dump(plan, meta, body)
    r = run(["--json", "feature", "show", "1"])
    assert r.json["open_tasks"] == 2
    assert r.json["done_tasks"] == 1
    assert r.json["next_task"] == "do two"


def test_current_without_session(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["feature", "current"])
    assert r.code == 2
    assert "no session id" in r.err
