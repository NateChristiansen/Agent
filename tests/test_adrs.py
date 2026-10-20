import shutil
from pathlib import Path

import pytest

from agent_cli import frontmatter
from harness_root import link_notes

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_new_adr_numbering_and_body(tmp_root: Path, run):
    r1 = run(["--json", "adr", "new", "first", "--title", "First", "--scope", "harness"])
    r2 = run(
        ["--json", "adr", "new", "second", "--title", "Second", "--status", "accepted", "--scope", "harness"]
    )
    assert r1.json["file"] == "adrs/0001-first.md"
    assert r2.json["file"] == "adrs/0002-second.md"
    meta, body = frontmatter.load(tmp_root / "adrs/0002-second.md")
    assert meta["status"] == "accepted"
    assert str(meta["date"]) == "2026-09-03"
    assert "feature" not in meta
    assert body.startswith("# ADR-0002: Second\n")


def test_feature_link_updates_plan(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["--json", "adr", "new", "arch", "--title", "Arch", "--feature", "x", "--scope", "harness"])
    assert r.json["feature"] is None
    meta, _ = frontmatter.load(tmp_root / "features/001-x/plan.md")
    assert meta["adrs"] == [1]


def test_supersedes_links_both_sides(tmp_root: Path, run):
    run(["adr", "new", "old", "--title", "Old", "--status", "accepted", "--scope", "harness"])
    r = run(["--json", "adr", "new", "new", "--title", "New", "--supersedes", "1", "--scope", "harness"])
    assert r.json["supersedes"] == 1
    old, _ = frontmatter.load(tmp_root / "adrs/0001-old.md")
    assert old["superseded_by"] == 2
    assert old["status"] == "superseded"
    assert run(["adr", "new", "x", "--title", "X", "--supersedes", "99", "--scope", "harness"]).code == 1


def test_adr_index_rendered(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["adr", "new", "arch", "--title", "Arch", "--feature", "1", "--scope", "harness"])
    index = (tmp_root / "adrs/README.md").read_text()
    assert "[0001](0001-arch.md)" in index
    assert "Feature" not in index


def test_adr_index_does_not_link_the_private_features(tmp_root: Path, run):
    """adrs/README.md is tracked in the harness repo and features/ is the owner's (ADR-0012).
    A link to the feature made the file differ between the work vault and a public clone, so
    it was dirty after every render here and dangled there."""
    run(["feature", "new", "x", "--title", "X"])
    run(["adr", "new", "arch", "--title", "Arch", "--feature", "1", "--scope", "harness"])

    def rows() -> list[str]:
        text = (tmp_root / "adrs/README.md").read_text()
        return [ln for ln in text.splitlines() if ln.startswith("| [")]

    with_notes = rows()
    assert with_notes and all("../features/" not in ln for ln in with_notes)

    shutil.rmtree(tmp_root / "features")
    run(["status", "render"])
    assert rows() == with_notes, "the ADR table reads the same without the notes repo"


def test_adr_index_is_byte_identical_after_a_feature_only_change(tmp_root: Path, run):
    """The Feature cell was only half of it: the file was also stamped with `source_hash`,
    which covers every feature's frontmatter as well as the ADRs. So a ticked task or a log
    entry rewrote the hash line of a file whose table had not changed — adrs/README.md is
    tracked here while features/ is private, so it went dirty on any work at all, carrying a
    digest a public clone could never reproduce. It carries `adr_hash` now: the ADRs alone."""
    run(["feature", "new", "x", "--title", "X"])
    run(["adr", "new", "arch", "--title", "Arch", "--feature", "1", "--scope", "harness"])
    index = tmp_root / "adrs/README.md"
    before = index.read_text()

    run(["task", "add", "1", "ship it"])
    run(["task", "done", "1", "1"])
    run(["feature", "new", "y", "--title", "Y"])
    run(["status", "render"])
    assert index.read_text() == before, "a feature-only change must not touch the ADR index"

    run(["adr", "new", "second", "--title", "Second", "--scope", "harness"])
    assert index.read_text() != before, "a new ADR still changes it"


def test_private_default_requires_linked_notes(tmp_root: Path, run):
    result = run(["adr", "new", "work", "--title", "Work"])
    assert result.code != 0
    assert not (tmp_root / "adrs/0001-work.md").exists()


def test_private_decision_numbering_index_and_feature_link(tmp_root: Path, run):
    notes = link_notes(tmp_root)
    run(["feature", "new", "x", "--title", "X"])
    run(["adr", "new", "public", "--title", "Public", "--scope", "harness"])
    result = run(["--json", "adr", "new", "work", "--title", "Work", "--feature", "1"])
    assert result.code == 0, result.out
    assert result.json["scope"] == "notes"
    assert result.json["file"] == "decisions/0001-work.md"
    assert (notes / "decisions/0001-work.md").exists()
    meta, _ = frontmatter.load(notes / "features/001-x/plan.md")
    assert meta["decisions"] == [1]
    assert meta["adrs"] == []
    assert "[[decisions/0001-work|ADR-0001]]" in meta["links"]
    assert "[0001](0001-work.md)" in (notes / "decisions/README.md").read_text()
