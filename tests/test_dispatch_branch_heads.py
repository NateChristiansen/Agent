"""Fresh and reused dispatch worktrees start from origin's exact branch head, against real Git."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cli.core import AgentError, ExternalFailure
from agent_cli.dispatch.worktrees import ensure_worktree, reuse_worktree
from agent_cli.repos import Repo


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def head(path: Path, ref: str = "HEAD") -> str:
    return git(path, "rev-parse", ref)


def commit(path: Path, value: str) -> str:
    (path / "tracked").write_text(value)
    git(path, "add", "tracked")
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", value)
    return head(path)


@pytest.fixture
def product(tmp_path: Path) -> tuple[Path, Path, Path, Repo]:
    """A product clone with an origin carrying `topic`, a second clone that pushes to it, and a
    separate manager root that owns the allocation guards."""
    main = tmp_path / "Product"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    commit(main, "initial")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", "--no-local", str(main), str(origin))
    git(main, "remote", "add", "origin", str(origin))
    git(main, "fetch", "-q", "origin")
    git(main, "branch", "topic")
    git(main, "push", "-q", "origin", "topic")
    upstream = tmp_path / "upstream"
    git(tmp_path, "clone", "-q", "--no-local", str(origin), str(upstream))
    git(upstream, "checkout", "-q", "topic")
    manager = tmp_path / "Manager"
    manager.mkdir()
    return main, upstream, manager, Repo(name="Product", short="P", path=str(main), remote=str(origin))


def advance_remote(upstream: Path, value: str) -> str:
    tip = commit(upstream, value)
    git(upstream, "push", "-q", "origin", "topic")
    return tip


@pytest.mark.parametrize("remote_only", [False, True])
def test_equal_and_remote_only_branches_start_at_the_remote_head(product, remote_only: bool):
    main, _, manager, rp = product
    expected = head(main, "topic")
    if remote_only:
        git(main, "branch", "-D", "topic")
    path = main.parent / "wt"
    assert ensure_worktree(manager, rp, path, "topic", "origin/main")["base"] == expected
    assert head(path) == expected
    assert git(path, "symbolic-ref", "--short", "HEAD") == "topic"


@pytest.mark.parametrize("reuse", [False, True])
def test_a_clean_behind_local_branch_is_fast_forwarded(product, reuse: bool):
    main, upstream, manager, rp = product
    path = main.parent / "wt"
    if reuse:
        git(main, "worktree", "add", "-q", str(path), "topic")
    remote = advance_remote(upstream, "remote advance")
    result = (reuse_worktree if reuse else ensure_worktree)(manager, rp, path, "topic", "origin/main")
    assert result["base"] == remote
    assert head(path) == remote and head(main, "topic") == remote


def test_reuse_moves_a_clean_worktree_onto_a_behind_branch(product):
    main, upstream, manager, rp = product
    path = main.parent / "wt"
    git(main, "worktree", "add", "-q", "-b", "older", str(path), "main")
    remote = advance_remote(upstream, "remote advance")
    result = reuse_worktree(manager, rp, path, "topic", "origin/main")
    assert result["base"] == remote and result["reused"]
    assert git(path, "symbolic-ref", "--short", "HEAD") == "topic" and head(path) == remote


def test_a_local_only_branch_is_kept_once_origin_confirms_it_has_none(product):
    main, _, manager, rp = product
    git(main, "checkout", "-q", "-b", "local-only")
    local = commit(main, "local work")
    git(main, "checkout", "-q", "main")
    path = main.parent / "wt"
    assert ensure_worktree(manager, rp, path, "local-only", "origin/main")["base"] == local
    assert head(path) == local


def test_a_new_branch_starts_from_the_fetched_base(product):
    main, _, manager, rp = product
    other = main.parent / "pusher"
    git(main.parent, "clone", "-q", "--no-local", rp.remote, str(other))
    pushed = commit(other, "main advance")
    git(other, "push", "-q", "origin", "main")
    path = main.parent / "wt"
    assert ensure_worktree(manager, rp, path, "agent/new", "origin/main")["base"] == pushed
    assert head(path) == pushed


@pytest.mark.parametrize("ahead", [True, False])
def test_ahead_or_diverged_branches_are_refused_without_mutation(product, ahead: bool):
    main, upstream, manager, rp = product
    if not ahead:
        advance_remote(upstream, "remote advance")
    git(main, "checkout", "-q", "topic")
    local = commit(main, "local advance")
    git(main, "checkout", "-q", "main")
    path = main.parent / "wt"
    with pytest.raises(AgentError, match="ahead of or diverges"):
        ensure_worktree(manager, rp, path, "topic", "origin/main")
    assert head(main, "topic") == local
    assert not path.exists()


def test_a_dirty_reused_worktree_is_preserved(product):
    main, upstream, manager, rp = product
    path = main.parent / "wt"
    git(main, "worktree", "add", "-q", str(path), "topic")
    before = head(path)
    advance_remote(upstream, "remote advance")
    (path / "dirty").write_text("preserve")
    with pytest.raises(AgentError, match="uncommitted"):
        reuse_worktree(manager, rp, path, "topic", "origin/main")
    assert (path / "dirty").read_text() == "preserve"
    assert head(path) == before and head(main, "topic") == before


def test_an_occupied_branch_is_refused_without_mutation(product):
    main, upstream, manager, rp = product
    occupied = main.parent / "occupied"
    git(main, "worktree", "add", "-q", str(occupied), "topic")
    expected = head(occupied)
    advance_remote(upstream, "remote advance")
    with pytest.raises(AgentError, match="already checked out"):
        ensure_worktree(manager, rp, main.parent / "other", "topic", "origin/main")
    reused = main.parent / "reused"
    git(main, "worktree", "add", "-q", "-b", "spare", str(reused), "main")
    with pytest.raises(AgentError, match="already checked out"):
        reuse_worktree(manager, rp, reused, "topic", "origin/main")
    assert head(occupied) == expected and head(main, "topic") == expected
    assert not (main.parent / "other").exists()
    assert git(reused, "symbolic-ref", "--short", "HEAD") == "spare"


def test_an_unanswered_remote_query_is_not_absence(product):
    main, _, manager, rp = product
    git(main, "branch", "-D", "topic")
    Path(rp.remote).rename(Path(rp.remote).with_name("gone.git"))
    path = main.parent / "unknown"
    with pytest.raises(ExternalFailure):
        ensure_worktree(manager, rp, path, "topic", "origin/main")
    assert not path.exists()
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/topic"], cwd=main
    ).returncode
    reused = main.parent / "reused"
    git(main, "worktree", "add", "-q", "-b", "spare", str(reused), "main")
    with pytest.raises(ExternalFailure):
        reuse_worktree(manager, rp, reused, "topic", "origin/main")
    assert git(reused, "symbolic-ref", "--short", "HEAD") == "spare"


def test_a_resume_keeps_its_branch_and_commits(product):
    main, upstream, manager, rp = product
    path = main.parent / "wt"
    ensure_worktree(manager, rp, path, "topic", "origin/main")
    work = commit(path, "worker commit")
    again = ensure_worktree(manager, rp, path, "topic", "origin/main", resume=True)
    assert again["created"] is False and head(path) == work
    git(main, "worktree", "remove", str(path))
    advance_remote(upstream, "remote advance")
    restored = ensure_worktree(manager, rp, path, "topic", "origin/main", resume=True)
    assert restored["created"] is True and head(path) == work == head(main, "topic")
