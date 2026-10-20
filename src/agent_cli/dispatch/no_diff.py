"""Exempt an unchanged artifact from supervisor verification, using Git observations rather than the
worker's claim.

Require recorded start_sha, unchanged HEAD, clean index and tracked files including modes and
deletions, and no non-ignored untracked files. An empty commit or commit-and-revert changes HEAD
and cannot qualify. Ignored bytes are outside this claim. Acceptance rechecks every observation,
and an exemption never counts as a full-suite pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

NO_DIFF = "no-diff: unchanged from the dispatch start (supervisor observed)"
NO_RECORD = "unchanged; no applicable full verification"


@dataclass(frozen=True)
class Observation:
    head: str | None
    clean: bool
    reason: str


def observe(worktree: Path, launch_base: str | None) -> Observation:
    """HEAD and whether index, tracked files and non-ignored untracked files are all clean.
    Any failed observation, or a tree layout the observer refuses, reads as not clean.
    `launch_base` is the dispatch's `start_sha`, which a repo with submodules needs."""
    from .verification_process import hardened_argv, observe_tree, untracked_clean

    try:
        argv = hardened_argv(worktree)
        tree = observe_tree(worktree, launch_base=launch_base, argv=argv)
        untracked = untracked_clean(worktree, argv=argv)
    except Exception as exc:  # noqa: BLE001 — unknown state never grants an exemption
        return Observation(None, False, f"observation failed ({type(exc).__name__})")
    if not (tree.tracked_clean and tree.index_clean):
        return Observation(tree.head, False, "tracked or staged changes")
    if not untracked:
        return Observation(tree.head, False, "untracked files")
    return Observation(tree.head, True, "clean")


def eligible(worktree: Path, start_sha: str | None) -> Observation | None:
    """The observation when the dispatch is unchanged from `start_sha`, else None."""
    if not start_sha:
        return None
    seen = observe(worktree, start_sha)
    return seen if seen.clean and seen.head == start_sha else None


def relied_on(root: Path, worktree: Path, start_sha: str | None) -> str:
    """The full pass record that applies to `start_sha`'s tree, for the exempt result to name, or
    `NO_RECORD`. Records cover the harness repo only; a failed lookup reads as none."""
    from .. import verify_record

    if not start_sha or not verify_record.covers(root, worktree):
        return NO_RECORD
    try:
        found, _ = verify_record.lookup(root, start_sha)
    except Exception:  # noqa: BLE001 — naming a record is a report, never a grant
        return NO_RECORD
    return verify_record.pointer(found) if found is not None else NO_RECORD
