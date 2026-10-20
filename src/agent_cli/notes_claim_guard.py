"""Keeps auto-sync away from the claims ledger (see ADR-0040).

`records/claims.jsonl` is written by the claim tool alone, as a compare-and-swap commit on the
notes remote. A sync commit that carried it could overwrite that ledger, so `refusal` stops the
sync both when the file shows local changes and when it is already staged, and it fails closed:
a git read that fails is a refusal, never a clean file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .notes_sync import Repo

CLAIMS_FILE = "records/claims.jsonl"
CLAIMS = f":(glob)**/{CLAIMS_FILE}"
NOT_CLAIMS = f":(exclude,glob)**/{CLAIMS_FILE}"
LABELS_FILE = "records/labels.jsonl"
LABELS = f":(glob)**/{LABELS_FILE}"
NOT_LABELS = f":(exclude,glob)**/{LABELS_FILE}"


def refusal(repo: Repo, staged: str | None = None) -> str | None:
    """Why a sync must not go on, or None. Without `staged`, reads the claims files' status; with
    `staged`, the newline-separated names about to be committed."""
    if staged is not None:
        names = [
            p
            for p in staged.splitlines()
            if any(p == f or p.endswith(f"/{f}") for f in (CLAIMS_FILE, LABELS_FILE))
        ]
        return (
            f"{names[0]} is staged; only the claim tool writes it (`git restore --staged {names[0]}`)"
            if names
            else None
        )
    status = repo.out("status", "--porcelain", "--untracked-files=all", "--", CLAIMS)
    if status is None:
        return "could not read the claims file status; nothing was committed"
    if not status:
        status = repo.out("status", "--porcelain", "--untracked-files=all", "--", LABELS)
        if status is None:
            return "could not read the label claims file status; nothing was committed"
        if not status:
            return None
    path = status.splitlines()[0].split()[-1]
    return f"{path} has local changes; only the claim tool writes it (`git checkout -- {path}` restores it)"
