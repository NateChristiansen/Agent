from pathlib import Path

import pytest

from agent_cli import frontmatter

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_new_feature_and_adr_get_tags_and_links(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X", "--tag", "ui"])
    meta, _ = frontmatter.load(tmp_root / "features/001-x/plan.md")
    assert meta["tags"] == ["feature", "ui"]
    assert meta["links"] == []
    run(["adr", "new", "arch", "--title", "Arch", "--feature", "1", "--scope", "harness"])
    fmeta, _ = frontmatter.load(tmp_root / "features/001-x/plan.md")
    ameta, _ = frontmatter.load(tmp_root / "adrs/0001-arch.md")
    assert fmeta["links"] == ["[[adrs/0001-arch|ADR-0001]]"]
    assert ameta["tags"] == ["adr"]
    assert ameta["links"] == []
    assert "feature" not in ameta


def test_supersedes_links_both_adrs(tmp_root: Path, run):
    run(["adr", "new", "old", "--title", "Old", "--scope", "harness"])
    run(["adr", "new", "new", "--title", "New", "--supersedes", "1", "--scope", "harness"])
    old, _ = frontmatter.load(tmp_root / "adrs/0001-old.md")
    new, _ = frontmatter.load(tmp_root / "adrs/0002-new.md")
    assert old["links"] == ["[[adrs/0002-new|ADR-0002]]"]
    assert new["links"] == ["[[adrs/0001-old|ADR-0001]]"]


def test_type_tag_cannot_be_removed(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["--json", "feature", "set", "1", "--remove-tag", "feature", "--add-tag", "z"])
    assert r.json["tags"] == ["feature", "z"]
