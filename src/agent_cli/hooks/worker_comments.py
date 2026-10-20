"""Refuse a dispatch worker's commit that would add comment noise.

The convention gate reads comments only after an attempt ends, so a worker that scattered
boilerplate paid for the whole attempt and then another to clean it up: one dispatch returned 41
added comments, 33 of them one line repeated across test modules, and was blocked near the end
of its budget. This check runs at `git commit` on the result the commit would leave: every line
the dispatch's own commits added since it started, plus what the commit takes in now. Three
one-file commits cannot hide a repeat across three files.

What counts as noise is `dispatch/comment_noise.py`, shared with the return gate: a comment or
docstring that cites a feature, lesson or dispatch id, and the same text added in three or more
files. A test's Given/When/Then lines and tool directives such as `# fmt: skip` are exempt.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .. import repos as repos_mod
from ..dispatch.comment_noise import added_docstrings, noisy
from ..dispatch.conventions import added_comments, parse_diff
from .commit_args import takes_worktree
from .paths import PROTECTED_NOTE
from .worker_redirect import newlines_as_separators, segments

#: git options that take the next word as their value, before the subcommand.
_GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _commit_args(words: list[str]) -> list[str] | None:
    """The arguments after `commit` when `words` runs `git commit`, else None."""
    words = list(words)
    while words and "=" in words[0] and not words[0].startswith("-"):
        words.pop(0)
    if not words or Path(words[0]).name != "git":
        return None
    words = words[1:]
    while words and words[0].startswith("-"):
        option = words.pop(0)
        if option in _GIT_VALUE_OPTIONS and words:
            words.pop(0)
    if not words or words[0] != "commit":
        return None
    return words[1:]


def _git(args: list[str], worktree: Path) -> str:
    out = repos_mod.git_or_none(args, worktree)
    if out is None:
        raise RuntimeError(f"git {' '.join(args)} failed in {worktree}")
    return out


def _history_lines(worktree: Path, start: str) -> set[tuple[str, str]]:
    """The lines `own_lines` reads, but a failed `git log` raises instead of reading as no
    history, so the check refuses rather than allows."""
    out = _git(
        [
            "log", "--first-parent", "--no-merges", "--format=", "-p", "--no-color", "--no-ext-diff",
            "--unified=0", "--no-renames", f"{start}..HEAD",
        ],
        worktree,
    )  # fmt: skip
    return {(f.path, text) for f in parse_diff(out) for _, text in f.added}


def committing_comments(worktree: Path, start: str | None, from_worktree: bool) -> list[dict[str, Any]]:
    """Every comment the branch would add once the commit lands: lines in the commit's result
    that differ from `start` and that either the dispatch's own commits or this commit wrote.
    A line main brought in through a merge is in neither, so it is left out as the gate leaves
    it out. Without `start` only the commit's own lines are read."""
    diff = ["diff", "--no-color", "--no-ext-diff", "--unified=0", "--no-renames"]
    head = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], worktree)
    pending = diff + (["HEAD"] if from_worktree else ["--cached"]) if head else None
    own: set[tuple[str, str]] = set()
    if pending:
        own = {(f.path, text) for f in parse_diff(_git(pending, worktree)) for _, text in f.added}
        if repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", "MERGE_HEAD^{commit}"], worktree):
            against_other = diff + (["MERGE_HEAD"] if from_worktree else ["--cached", "MERGE_HEAD"])
            own &= {(f.path, text) for f in parse_diff(_git(against_other, worktree)) for _, text in f.added}
    if start:
        own |= _history_lines(worktree, start)
        result = diff + ([start] if from_worktree else ["--cached", start])
    elif pending:
        result = pending
    else:
        return []
    files = parse_diff(_git(result, worktree))
    for f in files:
        f.added = [(n, text) for n, text in f.added if (f.path, text) in own]

    def read(path: str) -> str | None:
        if from_worktree:
            try:
                return (worktree / path).read_text(encoding="utf-8")
            except OSError, ValueError:
                return None
        return repos_mod.git_or_none(["show", f":{path}"], worktree)

    return added_comments(files) + added_docstrings(files, read)


def reason_for(rows: list[dict[str, Any]]) -> str:
    lines = [f'- {r["path"]}:{r["line"]} {r["why"]}: "{r["text"][:100]}"' for r in rows]
    return "\n".join(
        [
            "This commit would add comment noise:",
            *lines,
            "Delete each comment, or keep one only if it tells a reader something the code does "
            "not. Feature, task, lesson and dispatch ids belong in your return, not in the code. "
            "Unstage or edit the files, then commit again.",
            PROTECTED_NOTE,
        ]
    )


def comment_noise(command: str, worktree: Path, start: str | None) -> str | None:
    """Why `command` would commit comment noise, or None. Not a commit, or unparseable input,
    passes. A git failure raises, so the caller refuses the commit."""
    try:
        lexer = shlex.shlex(newlines_as_separators(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    for words in segments(tokens):
        args = _commit_args(words)
        if args is None:
            continue
        if rows := noisy(committing_comments(worktree, start, takes_worktree(args))):
            return reason_for(rows)
    return None
