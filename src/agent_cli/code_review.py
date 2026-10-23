"""Code review in blind rounds: two fresh, read-only reviewers (Codex and Claude), then one
verifier, all pinned to one branch head.

The `dispatch` action `code-review` runs the round (`ops/code_review.py`) with plan review's
engine. Where a plan round is keyed by the plan's bytes, a code round is keyed by the branch head
sha at launch: every dispatch of the round checks out that commit, so a later commit cannot
change what it reads, and a moved head starts a new round. Under `.agent/plan-reviews/code/`, a
round record is `<head>-<round>.json`, the frozen acceptance `acceptance/<sha256>.md`, and the
findings ledger, one per branch, `ledgers/<branch-key>.json`.

`clean` is what `land` and `push-guard` ask of every branch head (ADR-0063): is the latest round
on exactly this head complete with no verified blocking finding. This module only reads records:
a hook may import it.
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


def unclean(root: Path, *, branch: str, head: str, repo: str | None = None) -> str | None:
    """Why `head` has no clean round, in words; None when `clean` holds."""
    rec = latest(root, branch=branch, head=head, repo=repo)
    if rec is None:
        older = records(root, branch=branch, repo=repo)
        if older:
            return f"its rounds are on older heads (the latest on {str(older[-1].get('head'))[:12]})"
        return "no code-review round has run on it"
    if round_clean(rec):
        return None
    label = f"round {rec.get('round')}"
    phase = rec.get("phase")
    if phase in ("reviewing", "verifying"):
        return f"{label} is still {phase}"
    if complete(rec) and plan_review.blocking(rec):
        return f"{label} kept verified blocking findings: {', '.join(plan_review.blocking(rec))}"
    if complete(rec):
        return f"{label} is complete but its blocking list is unreadable ({rec.get('blocking')!r})"
    return f"{label} is in an unknown phase {phase!r}"


def unreadable(root: Path, head: str) -> list[str]:
    """The round files of `head` that are not a readable JSON object."""
    return [p.name for p in _round_files(root, head) if not plan_review.read_records([p])]


def malformed(root: Path, head: str) -> list[str]:
    """The round files of `head` that read as an object without its identity: no branch, or a
    head other than the one its name carries."""
    return [
        p.name
        for p in _round_files(root, head)
        for r in plan_review.read_records([p])
        if not (isinstance(r.get("branch"), str) and r["branch"] and r.get("head") == head)
    ]


def _round_files(root: Path, head: str) -> list[Path]:
    base = reviews_dir(root)
    return sorted(base.glob(f"{head}-*.json")) if base.is_dir() else []
