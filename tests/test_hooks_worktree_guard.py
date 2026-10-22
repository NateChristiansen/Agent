"""`protect-generated` hardening on top of an earlier manager-worktree branch (008 review).

`tests/test_hooks_worktree_edits.py` covers the ordinary cases: edits in a worktree pass,
CLI-owned files and `repos/` are refused, a look-alike sibling of the parent asks. These cover
what a lexical check alone misses: a child of the parent that is absent or is not a git
worktree of this repository, and a symlink whose written spelling hides where the write lands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_cli import links
from agent_cli.hooks import guards
from hooks_test_helpers import _decision, edit, manager_worktrees


@pytest.fixture
def worktree(tmp_root: Path, monkeypatch: pytest.MonkeyPatch, run, notes_remote: Path) -> Path:
    """One real worktree under `../Agent-worktrees/`. The fixture lives under the temp dir,
    which the hook lets through on its own; that allowance is off so every decision here comes
    from the worktree check."""
    monkeypatch.setattr(guards, "TMP_PREFIXES", ())
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    assert run(["feature", "new", "guard-owner", "--title", "Guard owner"]).code == 0
    assert run(["feature", "use", "1", "--session-id", "manager"]).code == 0
    return manager_worktrees(tmp_root, "wt")[0]


def _hook(run, path: Path | str, session_id: str | None = None) -> str:
    payload = json.loads(edit("Write", str(path)))
    if session_id:
        payload["session_id"] = session_id
    return _decision(run(["hook", "protect-generated"], stdin=json.dumps(payload)))


def test_a_new_file_in_a_verified_worktree_needs_no_decision(worktree: Path, run):
    path = worktree / "new" / "file.py"
    assert not path.parent.exists()
    assert _hook(run, path, "manager") == ""


def test_a_file_under_a_child_that_does_not_exist_asks(worktree: Path, run):
    """Nothing there to verify, so nothing says it is a worktree of this repository."""
    assert _hook(run, worktree.parent / "made-up" / "x.py") == "ask"


def test_a_file_under_an_existing_directory_that_is_not_a_worktree_asks(worktree: Path, run):
    """A plain directory has no `.git`. A copied `.git` file names the real worktree's admin
    dir, whose back-pointer names the real worktree, not the copy."""
    plain = worktree.parent / "plain"
    plain.mkdir()
    fake = worktree.parent / "fake"
    fake.mkdir()
    (fake / ".git").write_text((worktree / ".git").read_text(encoding="utf-8"), encoding="utf-8")
    for path in (plain / "x.md", fake / "x.md"):
        assert _hook(run, path) == "ask", path


def test_traversal_out_of_a_worktree_still_asks(worktree: Path, run):
    assert _hook(run, f"{worktree}/../../x.md") == "ask"


def _link(at: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")
    try:
        at.symlink_to(target)
    except OSError:
        if sys.platform != "win32":
            raise
        pytest.skip("this Windows account cannot make file symlinks (no Developer Mode)")
    return at


@pytest.mark.parametrize("target", ["STATUS.md", ".agent/sessions/abc.json"])
def test_a_worktree_symlink_into_a_protected_main_file_is_denied(
    worktree: Path, tmp_root: Path, run, target: str
):
    """The written spelling (`ordinary`) is harmless; where it lands decides. The main
    checkout's native deny rules match the written path, so they never see this alias."""
    assert _hook(run, _link(worktree / "ordinary", tmp_root / target)) == "deny"


def test_a_main_symlink_onto_a_protected_main_file_is_denied(worktree: Path, tmp_root: Path, run):
    """The same alias inside the main checkout: the requested path differs from the resolved
    one, so the resolved one is judged."""
    assert _hook(run, _link(tmp_root / "ordinary.md", tmp_root / "STATUS.md")) == "deny"


def test_a_worktree_symlink_to_a_protected_file_in_the_same_worktree_is_denied(worktree: Path, run):
    assert _hook(run, _link(worktree / "ordinary", worktree / "STATUS.md")) == "deny"


def test_a_worktree_symlink_to_an_ordinary_file_outside_both_still_asks(worktree: Path, run):
    assert _hook(run, _link(worktree / "ordinary", worktree.parent.parent / "elsewhere" / "x.md")) == "ask"


def test_a_worktree_symlink_to_an_ordinary_main_file_matches_a_direct_write(
    worktree: Path, tmp_root: Path, run
):
    target = tmp_root / "notes.md"
    via_link = _hook(run, _link(worktree / "ordinary", target))
    assert via_link == _hook(run, target) == ""


def _dir_link(at: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    at.parent.mkdir(parents=True, exist_ok=True)
    links.make(at, target, is_dir=True)
    return at


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows collapses `link/..` by text before opening, so the escape does not exist",
)
def test_a_worktree_symlink_then_dotdot_onto_a_protected_main_file_is_denied(
    worktree: Path, tmp_root: Path, run
):
    """`wt/link/../ordinary`: collapsing `link/..` by text reads `wt/ordinary`, but the OS
    follows `link` into main first and lands on main's STATUS.md (008 review, 2026-10-04)."""
    _dir_link(worktree / "link", tmp_root / "subdir")
    _link(tmp_root / "ordinary", tmp_root / "STATUS.md")
    assert _hook(run, f"{worktree}/link/../ordinary") == "deny"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows collapses `link/..` by text before opening, so the escape does not exist",
)
def test_a_worktree_symlink_then_dotdot_onto_an_ordinary_file_outside_both_asks(worktree: Path, run):
    outside = worktree.parent.parent / "outside"
    _dir_link(worktree / "link", outside / "subdir")
    (outside / "ordinary.md").write_text("x\n", encoding="utf-8")
    assert _hook(run, f"{worktree}/link/../ordinary.md") == "ask"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows collapses `link/..` by text before opening, so the escape does not exist",
)
def test_a_main_symlink_then_dotdot_onto_status_is_denied(worktree: Path, tmp_root: Path, run):
    """`main/a/link/../STATUS.md` reads as `a/STATUS.md`, which no root-anchored native rule
    matches, and lands on main's STATUS.md."""
    _dir_link(tmp_root / "a" / "link", tmp_root / "b")
    (tmp_root / "STATUS.md").write_text("x\n", encoding="utf-8")
    assert _hook(run, f"{tmp_root}/a/link/../STATUS.md") == "deny"


def test_a_root_reached_through_a_symlink_denies_protected_files_and_passes_ordinary_ones(
    tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch, notes_remote: Path
):
    """Every requested path under a symlinked root differs from its resolved path, so the
    alias check sees them all: a CLI-owned file is denied (a second deny on top of Claude's
    native rule), and an ordinary file still passes."""
    monkeypatch.setattr(guards, "TMP_PREFIXES", ())
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    assert run(["feature", "new", "guard-owner", "--title", "Guard owner"]).code == 0
    assert run(["feature", "use", "1", "--session-id", "manager"]).code == 0
    alias = tmp_root.parent / "alias"
    links.make(alias, tmp_root, is_dir=True)
    monkeypatch.setenv("AGENT_ROOT", str(alias))
    monkeypatch.chdir(alias)
    assert _hook(run, alias / "STATUS.md") == "deny"
    assert _hook(run, alias / "src" / "x.py", "manager") == ""
