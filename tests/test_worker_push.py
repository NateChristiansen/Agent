"""A worker's push is judged by the git verb, not by the command text.

A worker's `git commit -m "... push-hook reads ..."` was denied twice by the native wildcard
`Bash(git * push*)`. The wildcard is gone; both providers' worker hooks parse the git call."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.dispatch import settings
from agent_cli.hooks.worker_push import git_push

PUSHES = [
    "git push",
    "git push origin main",
    "git -C /wt/alpha/001-x push origin main",
    "git -c core.hooksPath=/dev/null push origin HEAD",
    "git --git-dir=/wt/.git push",
    "FOO=1 git push --force",
]
NOT_PUSHES = [
    'git commit -q -m "config: narrow frontmatter, settings, manifest and push-hook reads"',
    "git log --grep push",
    'git commit -m "push"',
    "git status",
]


@pytest.mark.parametrize("command", PUSHES)
def test_every_push_spelling_is_refused(command: str, tmp_path: Path):
    assert git_push(command, tmp_path) is not None


@pytest.mark.parametrize("command", NOT_PUSHES)
def test_a_command_that_only_mentions_push_is_not(command: str, tmp_path: Path):
    assert git_push(command, tmp_path) is None


def test_the_native_wildcard_that_caught_commit_messages_is_gone():
    assert "Bash(git * push*)" not in settings.BASE_DENY
    assert "Bash(git push)" in settings.BASE_DENY and "Bash(git push:*)" in settings.BASE_DENY
