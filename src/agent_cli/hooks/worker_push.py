"""Parse git argv and refuse the push verb on both providers. Skip git options before the verb; quoted
commit text containing push is not a push operation.
"""

from __future__ import annotations

from pathlib import Path

REASON = "A worker never pushes: `git push` is the manager's step after review, with the owner's ask."


def git_push(command: str, worktree: Path) -> str | None:
    """The refusal reason when any git call in `command` is a push, else None."""
    from .commit_guard import git_calls

    return REASON if any(verb == "push" for _, verb, _, _ in git_calls(command, worktree)) else None
