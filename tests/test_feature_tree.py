"""One recursive walk finds standalone features and every feature inside an
initiative, at any depth, and refuses duplicate ids and containment cycles."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from agent_cli import feature_tree, features, frontmatter, sessions, templates
from agent_cli.core import AgentError
from agent_cli.journal.collect import _retros_today


def make_feature(root: Path, rel: str) -> Path:
    """A feature folder at `rel` (ending in NNN-slug) with the plan and log `features.new` writes."""
    fdir = root / rel
    m = features.DIR_RE.match(fdir.name)
    assert m, rel
    fid, slug = int(m.group(1)), m.group(2)
    fdir.mkdir(parents=True)
    meta = {
        "id": fid,
        "slug": slug,
        "title": slug.title(),
        "status": "active",
        "opened": date(2026, 10, 5),
        "closed": None,
        "tickets": [],
        "repos": [],
        "adrs": [],
        "decisions": [],
        "tags": ["feature"],
        "links": [],
    }
    frontmatter.dump(fdir / "plan.md", meta, templates.plan_body(f"{fid:03d}", meta["title"]))
    (fdir / "log.md").write_text(templates.log_header(f"{fid:03d}", slug), encoding="utf-8")
    return fdir


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")


def test_a_nested_feature_is_found_resolved_and_counted(tmp_root: Path):
    make_feature(tmp_root, "features/001-alone")
    nested = make_feature(tmp_root, "initiatives/01-goal/features/002-member")

    assert features.feature_dirs(tmp_root) == [tmp_root / "features/001-alone", nested]
    assert [f.id for f in features.load_all(tmp_root)] == [1, 2]
    assert features.next_id(tmp_root) == 3
    for ref in ("2", "002", "002-member", "member"):
        assert features.resolve(tmp_root, ref).dir == nested, ref
    f = features.resolve(tmp_root, "2")
    assert f.rel_dir(tmp_root) == "initiatives/01-goal/features/002-member"
    assert f.raw_dir == tmp_root / ".agent" / "raw" / "002-member"


def test_a_grandchild_initiative_feature_is_found(tmp_root: Path):
    make_feature(tmp_root, "features/001-alone")
    make_feature(tmp_root, "initiatives/01-goal/features/004-member")
    deep = make_feature(tmp_root, "initiatives/01-goal/initiatives/02-child/features/007-deep")

    tree = feature_tree.walk(tmp_root)
    assert [(i.id, i.slug, i.parent) for i in tree.initiatives] == [(1, "goal", None), (2, "child", 1)]
    assert features.next_id(tmp_root) == 8
    assert [item.id for item in tree.initiatives] == [1, 2]
    f = features.resolve(tmp_root, "deep")
    assert f.dir == deep
    assert f.raw_dir == tmp_root / ".agent" / "raw" / "007-deep"


def test_initiative_ids_are_their_own_sequence(tmp_root: Path):
    assert feature_tree.walk(tmp_root).initiatives == ()
    make_feature(tmp_root, "features/030-alone")
    (tmp_root / "initiatives/04-goal").mkdir(parents=True)
    assert [item.id for item in feature_tree.walk(tmp_root).initiatives] == [4]
    assert features.next_id(tmp_root) == 31


def test_current_resolves_a_nested_feature(tmp_root: Path):
    nested = make_feature(tmp_root, "initiatives/01-goal/features/002-member")
    sessions.set_record(
        tmp_root, "s1", feature={"id": 2, "slug": "member", "dir": "initiatives/01-goal/features/002-member"}
    )
    assert features.resolve(tmp_root, "current", session_id="s1").dir == nested


def test_a_duplicate_feature_id_anywhere_is_refused(tmp_root: Path):
    make_feature(tmp_root, "features/003-alone")
    make_feature(tmp_root, "initiatives/01-goal/initiatives/02-child/features/003-again")

    with pytest.raises(AgentError, match="duplicate feature id 003") as exc:
        features.next_id(tmp_root)
    assert "features/003-alone" in exc.value.message
    assert "initiatives/01-goal/initiatives/02-child/features/003-again" in exc.value.message
    with pytest.raises(AgentError, match="duplicate feature id"):
        features.resolve(tmp_root, "alone")


def test_a_duplicate_initiative_id_anywhere_is_refused(tmp_root: Path):
    (tmp_root / "initiatives/01-goal/initiatives/01-child").mkdir(parents=True)
    with pytest.raises(AgentError, match="duplicate initiative id 01"):
        feature_tree.walk(tmp_root)
    with pytest.raises(AgentError, match="duplicate initiative id 01"):
        features.load_all(tmp_root)


def test_a_containment_cycle_through_a_symlink_is_refused(tmp_root: Path):
    make_feature(tmp_root, "initiatives/01-goal/features/002-member")
    (tmp_root / "initiatives/01-goal/initiatives").mkdir()
    symlink_or_skip(tmp_root / "initiatives/01-goal/initiatives/02-loop", tmp_root / "initiatives/01-goal")
    with pytest.raises(AgentError, match="containment cycle: initiatives/01-goal/initiatives/02-loop"):
        features.load_all(tmp_root)


def test_new_refuses_a_slug_a_nested_feature_already_uses(tmp_root: Path):
    make_feature(tmp_root, "initiatives/01-goal/features/002-taken")
    with pytest.raises(AgentError, match="already used by 002-taken"):
        features.new(tmp_root, "taken", "Taken again")


def test_the_journal_finds_a_nested_retro(tmp_root: Path):
    fdir = make_feature(tmp_root, "initiatives/01-goal/features/002-member")
    frontmatter.dump(fdir / "retro.md", {"date": date(2026, 10, 5), "feature": 2}, "# Went well\n")
    rows = _retros_today(tmp_root, date(2026, 10, 5))
    assert rows == [
        {"feature": 2, "path": "initiatives/01-goal/features/002-member/retro.md", "headline": "Went well"}
    ]
