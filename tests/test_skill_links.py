"""The skill links setup makes instead of tracking them.

A Windows clone with `core.symlinks=false` checked every tracked symlink out as a plain file
holding its target path, so skills and caveman broke. These pin the replacement: setup makes
the links, repairs that plain-file placeholder, and never touches a real directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli import links, skill_links


def _tree(root: Path) -> Path:
    for name in ("adr", "intake"):
        (root / ".agents" / "skills" / name).mkdir(parents=True)
        (root / ".agents" / "skills" / name / "SKILL.md").write_text(f"# {name}\n")
    (root / ".agents" / "skills" / "LICENSE-obsidian-skills").write_text("MIT\n")
    caveman = root / "vendor" / "caveman" / "skills" / "caveman"
    caveman.mkdir(parents=True)
    (caveman / "SKILL.md").write_text("# caveman\n")
    return root


def _states(rows: list[dict]) -> dict[str, str]:
    return {Path(r["link"]).as_posix(): r["state"] for r in rows}


def test_expected_links_every_skill_dir_and_caveman_but_not_files(tmp_path: Path):
    root = _tree(tmp_path)
    links = [link.as_posix() for link, _ in skill_links.expected(root)]
    assert links == [
        ".agents/skills/caveman",
        ".claude/skills/adr",
        ".claude/skills/caveman",
        ".claude/skills/intake",
    ]


def test_ensure_makes_every_link_and_a_rerun_changes_nothing(tmp_path: Path):
    root = _tree(tmp_path)
    first = _states(skill_links.ensure(root))
    assert set(first.values()) == {"created"}
    assert (root / ".claude" / "skills" / "caveman" / "SKILL.md").read_text() == "# caveman\n"
    assert (root / ".claude" / "skills" / "adr" / "SKILL.md").read_text() == "# adr\n"
    assert set(_states(skill_links.ensure(root)).values()) == {"already"}
    assert set(_states(skill_links.status(root)).values()) == {"ok"}


def test_a_plain_placeholder_file_from_a_windows_clone_is_replaced(tmp_path: Path):
    """`core.symlinks=false` leaves a file whose content is the link target."""
    root = _tree(tmp_path)
    placeholder = root / ".claude" / "skills" / "adr"
    placeholder.parent.mkdir(parents=True)
    placeholder.write_text("../../.agents/skills/adr")
    assert _states(skill_links.status(root))[".claude/skills/adr"] == "plain"

    rows = _states(skill_links.ensure(root))
    assert rows[".claude/skills/adr"] == "replaced"
    assert (placeholder / "SKILL.md").read_text() == "# adr\n"


def test_a_real_file_that_is_not_the_placeholder_is_refused_and_kept(tmp_path: Path):
    """Only Git's exact placeholder is ours to replace. Anything else is someone's file."""
    root = _tree(tmp_path)
    mine = root / ".claude" / "skills" / "adr"
    mine.parent.mkdir(parents=True)
    mine.write_text("my own notes about adr\n")

    rows = _states(skill_links.ensure(root))
    assert rows[".claude/skills/adr"] == "refused"
    assert mine.read_text() == "my own notes about adr\n"


def test_a_real_directory_in_the_way_is_refused_and_kept(tmp_path: Path):
    root = _tree(tmp_path)
    real = root / ".claude" / "skills" / "intake"
    real.mkdir(parents=True)
    (real / "mine.md").write_text("keep me\n")

    rows = _states(skill_links.ensure(root))
    assert rows[".claude/skills/intake"] == "refused"
    assert (real / "mine.md").read_text() == "keep me\n"


def test_a_missing_target_is_skipped_without_leaving_a_dangling_link(tmp_path: Path):
    """No submodule checked out: the caveman link is not made, so it never dangles, and
    status leaves the cause to the submodule check."""
    root = _tree(tmp_path)
    for p in sorted((root / "vendor").rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()

    rows = _states(skill_links.ensure(root))
    assert rows[".agents/skills/caveman"] == "skipped"
    assert not skill_links.is_link(root / ".agents" / "skills" / "caveman")
    assert _states(skill_links.status(root))[".agents/skills/caveman"] == "no-target"


def test_status_reports_a_link_that_points_elsewhere(tmp_path: Path):
    root = _tree(tmp_path)
    skill_links.ensure(root)
    wrong = root / ".claude" / "skills" / "adr"
    skill_links._clear(wrong)  # rmdir for a Windows junction, unlink for a symlink
    links.make(wrong, root / ".agents" / "skills" / "intake", is_dir=True)
    assert _states(skill_links.status(root))[".claude/skills/adr"] == "wrong"
    assert _states(skill_links.ensure(root))[".claude/skills/adr"] == "replaced"


def test_session_start_links_a_skill_a_pull_added(tmp_root: Path, run):
    """A `git pull` that adds `.agents/skills/<name>/` brings no `.claude/skills/<name>` link:
    the links are gitignored and setup makes them, so Claude could not see the new skill until
    someone reran setup. Session start makes the link and says so."""
    from hooks_test_helpers import _ctx

    skill = tmp_root / ".agents" / "skills" / "fresh-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# fresh\n")
    link = tmp_root / ".claude" / "skills" / "fresh-skill"
    assert not skill_links.is_link(link)

    start = json.dumps({"session_id": "s1", "source": "startup"})
    r = run(["hook", "session-start"], stdin=start)
    assert r.code == 0
    assert skill_links.is_link(link)
    assert (link / "SKILL.md").read_text() == "# fresh\n"
    assert "made skill link(s): .claude/skills/fresh-skill" in _ctx(r)

    # linked already: nothing to say
    assert "skill link" not in _ctx(run(["hook", "session-start"], stdin=start))

    # a real directory where a link belongs is kept and reported, never replaced
    other = tmp_root / ".agents" / "skills" / "other-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("# other\n")
    (tmp_root / ".claude" / "skills" / "other-skill").mkdir(parents=True)
    r = run(["hook", "session-start"], stdin=start)
    assert r.code == 0
    assert ".claude/skills/other-skill refused" in _ctx(r)
    assert ".agents/skills/other-skill was not repaired" in _ctx(r)
    assert not skill_links.is_link(tmp_root / ".claude" / "skills" / "other-skill")


def test_session_start_reports_a_failed_or_unfinished_skill_link_repair(tmp_root: Path, run, monkeypatch):
    """The first review of the session-start repair found it swallowed an `ensure` that raised,
    which hid the very gap it exists to close. Start stays nonfatal and says so."""
    from hooks_test_helpers import _ctx

    start = json.dumps({"session_id": "s1", "source": "startup"})
    failed = {"link": ".claude/skills/x", "target": ".agents/skills/x", "state": "failed", "detail": "EACCES"}
    monkeypatch.setattr(skill_links, "ensure", lambda root: [failed])
    r = run(["hook", "session-start"], stdin=start)
    assert r.code == 0
    assert ".claude/skills/x failed: EACCES" in _ctx(r)

    def boom(root):
        raise RuntimeError("Symlink loop from '.claude/skills/x'")

    monkeypatch.setattr(skill_links, "ensure", boom)
    r = run(["hook", "session-start"], stdin=start)
    assert r.code == 0
    assert "did not finish (RuntimeError)" in _ctx(r)
