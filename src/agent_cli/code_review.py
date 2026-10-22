"""Code review in blind rounds: two fresh, read-only reviewers (Codex and Claude), then one
verifier, all pinned to one branch head.

The `dispatch` action `code-review` runs the round (`ops/code_review.py`) with plan review's
engine. Where a plan round is keyed by the plan's bytes, a code round is keyed by the branch head
sha at launch: every dispatch of the round checks out that commit, so a later commit cannot
change what it reads, and a moved head starts a new round. Under `.agent/plan-reviews/code/`, a
round record is `<head>-<round>.json`, the frozen acceptance `acceptance/<sha256>.md`, and the
findings ledger, one per branch, `ledgers/<branch-key>.json`.

`clean` is what landing asks: is the latest round on exactly this head complete with no verified
blocking finding. This module only reads records: a hook may import it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import plan_review
from .core import agent_path

# Inside the protected `plan-reviews` subtree, so a hand-written record cannot stand in for a review.
# `plan_review.all_records` reads only that directory's top level.
REVIEWS_DIR = agent_path(Path(), "plan-reviews", "code")
ACCEPTANCE_DIR = "acceptance"
LEDGER_DIR = "ledgers"
ROUND_CAP = plan_review.ROUND_CAP


def reviews_dir(root: Path) -> Path:
    return root / REVIEWS_DIR


def branch_key(repo: str, branch: str) -> str:
    return hashlib.sha256(f"{repo}\n{branch}".encode()).hexdigest()


def ledger_path(root: Path, repo: str, branch: str) -> Path:
    return reviews_dir(root) / LEDGER_DIR / f"{branch_key(repo, branch)}.json"


def acceptance_path(root: Path, sha: str) -> Path:
    return reviews_dir(root) / ACCEPTANCE_DIR / f"{sha}.md"


def round_path(root: Path, head: str, round_no: int) -> Path:
    return reviews_dir(root) / f"{head}-{round_no}.json"


def records(root: Path, *, branch: str, repo: str | None = None) -> list[dict[str, Any]]:
    """Every readable round of this branch, oldest launch first; any repo's when `repo` is None."""
    base = reviews_dir(root)
    if not base.is_dir():
        return []
    return [
        r
        for r in plan_review.read_records(sorted(base.glob("*.json")))
        if r.get("branch") == branch and (repo is None or r.get("repo") == repo)
    ]


def latest(root: Path, *, branch: str, head: str, repo: str | None = None) -> dict[str, Any] | None:
    """The newest round of this branch on exactly `head`, or None."""
    mine = [r for r in records(root, branch=branch, repo=repo) if r.get("head") == head]
    return mine[-1] if mine else None


def complete(rec: dict[str, Any]) -> bool:
    return rec.get("phase") == "complete"


def round_clean(rec: dict[str, Any]) -> bool:
    """A complete round whose verifier kept no finding as blocking."""
    return complete(rec) and isinstance(rec.get("blocking"), list) and not plan_review.blocking(rec)


def clean(root: Path, *, branch: str, head: str, repo: str | None = None) -> bool:
    """Is the latest round on exactly `head` complete with zero verified blocking findings. A round
    still running or stalled, or rounds only on an older head, are not clean."""
    rec = latest(root, branch=branch, head=head, repo=repo)
    return rec is not None and round_clean(rec)


def rounds_used(root: Path, *, branch: str, repo: str | None = None) -> int:
    """What `ROUND_CAP` counts for a branch: its complete rounds, on any head."""
    return sum(1 for r in records(root, branch=branch, repo=repo) if complete(r))
