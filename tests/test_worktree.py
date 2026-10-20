"""A checkout per manager over one shared `.agent`.

Worktrees exist because leases serialize edits and do not isolate breakage. On 2026-09-23 one
session left a `SyntaxError` in `dispatch/ledger.py` mid-edit and `bin/agent` stopped importing
for every session on the machine: hooks in both clients failed open, `doctor` died, and the peer channel went down with them, because the channel runs through the CLI being
edited. The message telling that session what it had broken had to go out through `codex queue`.

What a worktree must NOT isolate is coordination, which is what most of these tests are about.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import worktree
from agent_cli.core import AgentError


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
        env={
            "HOME": str(path),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin",
        },
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("worktree-template") / "Agent"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_text("#!/bin/sh\n")
    (root / ".agent").mkdir()
    (root / ".agent" / "sessions").mkdir()
    (root / ".gitignore").write_text(".agent/\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def repo(template: Path, tmp_path: Path) -> Path:
    """A git checkout shaped like the harness: `bin/agent` is the root marker, `.agent/` is the
    coordination state, and both must survive being worktree'd."""
    root = tmp_path / "Agent"
    shutil.copytree(template, root, symlinks=True)
    return root


def test_the_main_checkout_owns_the_state_and_a_worktree_links_to_it(repo: Path):
    """`.agent` is gitignored, so `git worktree add` hands a worktree an empty one. Sessions,
    leases, peers, dispatch records and live.json all live there: left alone, three managers
    become invisible to each other in `peer list` and leases arbitrate nothing."""
    assert worktree.is_worktree(repo) is False
    ok, detail = worktree.state_link_ok(repo)
    assert ok and "main checkout" in detail

    path = repo.parent / "Agent-wt-x"
    _git(repo, "worktree", "add", "-b", "agent/x", str(path), "main")
    assert worktree.is_worktree(path) is True
    assert worktree.main_checkout(path).resolve() == repo.resolve()

    # fresh from git: no `.agent` at all, and that is a failure rather than a warning
    ok, detail = worktree.state_link_ok(path)
    assert not ok and "worktree link" in detail

    result = worktree.link_state(path)
    assert result["linked"] is True
    ok, detail = worktree.state_link_ok(path)
    assert ok and str(repo / ".agent") in detail

    # the link is the point: state written in one checkout is read from the other
    (repo / ".agent" / "sessions" / "s1.json").write_text("{}")
    assert (path / ".agent" / "sessions" / "s1.json").exists()


def test_a_real_state_directory_in_a_worktree_fails_rather_than_warns(repo: Path):
    """The failure this check exists for is silent: a worktree with its own `.agent` works
    perfectly and coordinates with nobody. Nothing errors, `peer list` just shows one session."""
    path = repo.parent / "Agent-wt-y"
    _git(repo, "worktree", "add", "-b", "agent/y", str(path), "main")
    (path / ".agent").mkdir()
    ok, detail = worktree.state_link_ok(path)
    assert not ok
    assert "coordinates with itself" in detail
    assert "peer list, leases and dispatch ids" in detail

    # and nothing here deletes coordination state on the caller's behalf
    with pytest.raises(AgentError) as err:
        worktree.link_state(path)
    assert "move it aside yourself" in str(err.value)


def test_link_state_falls_back_to_the_shared_link_maker(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """On Windows `symlink_to` failed with WinError 1314 on an account without symlink
    rights. `links.make` falls back to a junction there; the worktree uses it too."""
    from agent_cli import links

    path = repo.parent / "Agent-wt-j"
    _git(repo, "worktree", "add", "-b", "agent/j", str(path), "main")
    made: list[tuple[Path, Path, bool]] = []
    real_make = links.make

    def junction_like(link: Path, target: Path, *, is_dir: bool) -> str:
        made.append((link, target, is_dir))
        real_make(link, target, is_dir=is_dir)  # a symlink where allowed, a junction otherwise
        return "junction"

    monkeypatch.setattr(links, "make", junction_like)
    result = worktree.link_state(path)
    assert result["kind"] == "junction" and made == [(path / ".agent", repo / ".agent", True)]


def test_link_state_is_idempotent_and_repoints_a_wrong_link(repo: Path):
    from agent_cli import links

    path = repo.parent / "Agent-wt-z"
    _git(repo, "worktree", "add", "-b", "agent/z", str(path), "main")
    assert worktree.link_state(path)["linked"] is True
    assert worktree.link_state(path)["linked"] is False  # already correct: a no-op, not an error

    elsewhere = repo.parent / "somewhere-else"
    elsewhere.mkdir()
    state = path / ".agent"
    state.unlink() if state.is_symlink() else state.rmdir()  # a junction is removed as a dir
    links.make(state, elsewhere, is_dir=True)
    ok, detail = worktree.state_link_ok(path)
    assert not ok and "not the main checkout's" in detail
    assert worktree.link_state(path)["linked"] is True
    assert worktree.state_link_ok(path)[0]


def test_the_main_checkout_may_not_link_its_own_state(repo: Path):
    """It owns the real directory. Linking it somewhere would leave every worktree pointing at
    a symlink to a symlink, and nothing owning the state."""
    with pytest.raises(AgentError) as err:
        worktree.link_state(repo)
    assert "owns" in str(err.value)


def test_a_worktree_lives_under_one_fixed_sibling_parent(repo: Path):
    """Outside the vault: every CLAUDE.md and AGENTS.md from cwd up to root loads into a
    session, so a checkout nested inside the vault inherits the manager context twice. Under
    one fixed parent: both clients can grant one literal directory, and a
    `../Agent-wt-<name>` per worktree was outside every grant, so Claude prompted on each edit
    and Codex refused the write. Separate from `../repos/`, so product guards never see it."""
    path = worktree.worktree_path(repo, "parity")
    assert path == repo.parent / "Agent-worktrees" / "parity"
    assert repo not in path.parents
    assert path.parent == worktree.worktrees_dir(repo)


def test_create_puts_the_worktree_under_the_parent(repo: Path):
    result = worktree.create(repo, "made", sync=False)
    path = repo.parent / "Agent-worktrees" / "made"
    assert Path(result["path"]) == path
    assert worktree.is_worktree(path)
    assert worktree.state_link_ok(path)[0]


def test_remove_keeps_the_branch_and_refuses_a_dirty_worktree(repo: Path):
    path = worktree.worktree_path(repo, "w")
    _git(repo, "worktree", "add", "-b", "agent/w", str(path), "main")
    (path / "scratch.txt").write_text("uncommitted work\n")

    with pytest.raises(AgentError) as err:
        worktree.remove(repo, "w")
    assert "uncommitted changes" in str(err.value)

    result = worktree.remove(repo, "w", force=True)
    assert result["removed"] and result["branch_kept"]
    assert not path.exists()
    # the branch outlives the worktree, because the work on it is the point and a worktree is
    # cheap to recreate. `create` then checks that branch out rather than the base ref — passing
    # the base there tried to check out `main`, which the main checkout already holds.
    assert "agent/w" in _git(repo, "branch", "--list", "agent/w")


def test_a_forced_remove_never_deletes_through_a_link(repo: Path):
    """041, 2026-10-12: `git worktree remove --force` on Windows recursed through the `.agent`
    junction and deleted live dispatch records. The notes-repo links were next in line."""
    from agent_cli import links

    path = worktree.worktree_path(repo, "l")
    _git(repo, "worktree", "add", "-b", "agent/l", str(path), "main")
    worktree.link_state(path)
    (repo / ".agent" / "sessions" / "live.json").write_text("{}")
    notes = repo.parent / "notes-target"
    (notes / "features").mkdir(parents=True)
    (notes / "features" / "plan.md").write_text("keep\n")
    links.make(path / "features", notes / "features", is_dir=True)
    (path / "scratch.txt").write_text("uncommitted work\n")

    try:
        result = worktree.remove(repo, "l", force=True)
    finally:  # checked even when git fails part-way: it may have deleted through a link first
        assert (repo / ".agent" / "sessions" / "live.json").exists()
        assert (notes / "features" / "plan.md").read_text() == "keep\n"
    assert not path.exists()
    assert sorted(Path(p).name for p in result["unlinked"]) == [".agent", "features"]


def test_a_clean_worktree_with_a_link_over_tracked_files_is_removed_unforced(repo: Path):
    """A link standing over a tracked directory with the same files reads clean. Removing the
    link before git runs leaves those files deleted, and git's own check refused the removal."""
    from agent_cli import links

    (repo / "tracked").mkdir()
    (repo / "tracked" / "keep.txt").write_text("keep\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "tracked dir")
    path = worktree.worktree_path(repo, "t")
    _git(repo, "worktree", "add", "-b", "agent/t", str(path), "main")
    outside = repo.parent / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n")
    (path / "tracked" / "keep.txt").unlink()
    (path / "tracked").rmdir()
    links.make(path / "tracked", outside, is_dir=True)
    if _git(path, "status", "--porcelain"):
        pytest.skip("git sees a symlink over a tracked directory; only a junction reads clean")

    result = worktree.remove(repo, "t")
    assert result["removed"] and not path.exists()
    assert (outside / "keep.txt").read_text() == "keep\n"


def test_listing_reports_each_worktree_and_whether_its_state_is_shared(repo: Path):
    linked = repo.parent / "Agent-wt-a"
    split = repo.parent / "Agent-wt-b"
    _git(repo, "worktree", "add", "-b", "agent/a", str(linked), "main")
    _git(repo, "worktree", "add", "-b", "agent/b", str(split), "main")
    worktree.link_state(linked)
    (split / ".agent").mkdir()

    rows = {Path(r["path"]).name: r for r in worktree.list_all(repo)}
    assert set(rows) == {"Agent-wt-a", "Agent-wt-b"}  # the main checkout is not listed
    assert rows["Agent-wt-a"]["state_ok"] is True
    assert rows["Agent-wt-a"]["manager"] is True
    assert rows["Agent-wt-b"]["state_ok"] is False


def test_a_name_with_a_space_is_refused(repo: Path):
    with pytest.raises(AgentError):
        worktree.create(repo, "two words")


@pytest.mark.parametrize(
    "name",
    ["team/one", "team\\one", "/abs", "trailing/", ".", "..", "../escape", "a/../b", "", "   "],
)
def test_a_name_must_be_one_directory_component(repo: Path, name: str):
    """The ask rules guarding hooks and the owner's rules match `<parent>/*/<path>`: one
    directory level. `team/one` landed a level deeper, outside every rule, and doctor then
    flagged it as misplaced; `..` escapes the parent entirely. Refused before any mkdir or git
    call, so a refused name leaves nothing behind."""
    before = _git(repo, "branch", "--list")
    siblings = sorted(p.name for p in repo.parent.iterdir())
    with pytest.raises(AgentError) as err:
        worktree.create(repo, name, sync=False)
    assert "one directory name" in str(err.value)
    assert not (repo.parent / "Agent-worktrees").exists()
    assert _git(repo, "branch", "--list") == before
    assert sorted(p.name for p in repo.parent.iterdir()) == siblings
    assert _git(repo, "worktree", "list").count("\n") == 0  # only the main checkout
