"""After a worker merges main, its commits are its own, not main's.

The record's commits come from git, so a merge of main must not add main's commits to them."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_cli.dispatch.evaluate import git_facts
from agent_cli.dispatch.worktrees import git_commits


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch_with_merged_main(tmp_path: Path) -> tuple[Path, str, str, str]:
    """`(repo, base, the worker's commit, main's later commit)`, with main merged into the branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "work")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "worker change")
    own = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "landed on main meanwhile")
    landed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "work")
    _git(repo, "merge", "-q", "--no-edit", "main")
    return repo, base, own, landed


def test_merged_in_main_commits_are_not_the_branchs(tmp_path: Path):
    repo, base, own, landed = _branch_with_merged_main(tmp_path)
    commits = git_commits(repo, base)
    shas = [c["sha"] for c in commits]
    assert own in shas and landed not in shas
    assert [c.get("merge") for c in commits] == ["1", None]


def test_git_facts_keep_the_branchs_own_commits_and_files(tmp_path: Path):
    repo, base, own, landed = _branch_with_merged_main(tmp_path)
    d: dict[str, Any] = {"worktree": str(repo), "base": base}
    facts = git_facts(d)
    assert facts is not None
    shas = [c["sha"] for c in facts["commits"]]
    assert own in shas and landed not in shas
    assert "a.txt" in facts["files"]


def test_git_facts_are_none_once_the_worktree_is_gone(tmp_path: Path):
    assert git_facts({"worktree": str(tmp_path / "gone"), "base": "HEAD"}) is None
