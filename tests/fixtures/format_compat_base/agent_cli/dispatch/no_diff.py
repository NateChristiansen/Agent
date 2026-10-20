# ruff: noqa
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
    from .verification_process import observe_tree, untracked_clean

    try:
        tree = observe_tree(worktree, launch_base=launch_base)
        untracked = untracked_clean(worktree)
    except Exception as exc:
        return Observation(None, False, f"observation failed ({type(exc).__name__})")
    if not (tree.tracked_clean and tree.index_clean):
        return Observation(tree.head, False, "tracked or staged changes")
    if not untracked:
        return Observation(tree.head, False, "untracked files")
    return Observation(tree.head, True, "clean")


def eligible(worktree: Path, start_sha: str | None) -> Observation | None:
    if not start_sha:
        return None
    seen = observe(worktree, start_sha)
    return seen if seen.clean and seen.head == start_sha else None


def relied_on(root: Path, worktree: Path, start_sha: str | None) -> str:
    from .. import verify_record

    if not start_sha or not verify_record.covers(root, worktree):
        return NO_RECORD
    try:
        found, _ = verify_record.lookup(root, start_sha)
    except Exception:
        return NO_RECORD
    return verify_record.pointer(found) if found is not None else NO_RECORD
