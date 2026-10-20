# ruff: noqa
from __future__ import annotations
from pathlib import Path

REASON = "A worker never pushes: `git push` is the manager's step after review, with the owner's ask."


def git_push(command: str, worktree: Path) -> str | None:
    from .commit_guard import git_calls

    return REASON if any((verb == "push" for _, verb, _, _ in git_calls(command, worktree))) else None
