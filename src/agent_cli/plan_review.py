"""Plan review: one fresh, read-only reviewer on the other client per plan version.

This replaces manager pairing. Two long-lived managers agreeing to a plan's hash needed a
second live manager, a pair record on both sessions and a Stop-hook reading chat for approval
asks; that hook blocked a Codex manager's trust question to the owner. Now the manager
launches the `dispatch` tool's `plan-review` action: the harness reads the plan once, freezes the
bytes as `.agent/plan-reviews/<sha>.md` (read-only), embeds them in the work order, and records
`<sha>.json`. `plan-gate` lets `ExitPlanMode` through only for a sha a finished review agreed
to, a plan that declares itself small, or after `ROUND_CAP` reviews of one plan file.

The hash is sha256 of the plan bytes with trailing newlines reduced to exactly one. ExitPlanMode's
`plan` text usually lacks the file's final newline, and an editor may add a second one; neither
changes what the owner reads. Nothing else is normalised, so any other edit voids a verdict.

The record is keyed by sha, so it names one version and its review alone. This module only reads
records and files: the hook imports it and must stay fast.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import agent_path
from .records.narrow import as_dict_or_none, dict_list

REVIEWS_DIR = agent_path(Path(), "plan-reviews")
ROUND_CAP = 3
AGREE = "VERDICT: agree"
REVISE = "VERDICT: revise"
MARKER_LINES = 15
#: `review: small — <reason>` or `review: none: <reason>` near the top: the plan opts out, and
#: says why. A bare `review: small` with no reason does not count.
EXEMPT_RE = re.compile(r"^\s*review:\s*(small|none)\b\s*[—:-]?\s*\S", re.IGNORECASE)
RUNNING_STATES = frozenset({"queued", "running"})
#: Rubric rows that build or design; `dispatch new` warns when one launches unreviewed.
IMPLEMENTATION_ROWS = frozenset({"build", "design", "max"})


def reviews_dir(root: Path) -> Path:
    return root / REVIEWS_DIR


def normalise(data: bytes) -> bytes:
    """The plan bytes the hash covers: trailing newlines reduced to exactly one."""
    return data.rstrip(b"\n") + b"\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(normalise(data)).hexdigest()


def resolve_plan(root: Path, path: str) -> Path:
    """A plan path as recorded: absolute and resolved; a relative one is under the vault root."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def exempt(text: str) -> str | None:
    """The `review: small|none` marker line in the plan's first lines, or None."""
    for line in text.splitlines()[:MARKER_LINES]:
        if EXEMPT_RE.match(line):
            return line.strip()
    return None


def record(root: Path, sha: str) -> dict[str, Any] | None:
    try:
        return as_dict_or_none(json.loads((reviews_dir(root) / f"{sha}.json").read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None


def all_records(root: Path) -> list[dict[str, Any]]:
    """Every readable review record, oldest launch first."""
    base = reviews_dir(root)
    if not base.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json")):
        try:
            data = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
        except OSError, ValueError:
            continue
        if data is not None:
            found.append(data)
    return sorted(found, key=lambda r: str(r.get("launched_at") or ""))


def records_for(root: Path, plan_path: str) -> list[dict[str, Any]]:
    """Every review record of this resolved plan path, oldest launch first."""
    return [r for r in all_records(root) if r.get("plan_path") == plan_path]


@dataclass(frozen=True)
class Verdict:
    """Where one review stands: `agree`, `revise`, `running`, or `none` (ended without a verdict,
    or its dispatch is missing). `detail` is the dispatch state or the summary's first line."""

    state: str
    dispatch_id: str
    detail: str = ""


def verdict(root: Path, rec: dict[str, Any]) -> Verdict:
    """Read the review's dispatch. A dispatch whose work order does not carry the record's sha
    is not that review, whatever the record says."""
    from .dispatch.state import load

    dispatch_id = str(rec.get("dispatch_id") or "")
    sha = str(rec.get("sha256") or "")
    try:
        d = load(root, dispatch_id)
    except Exception:  # noqa: BLE001 — a missing or unreadable dispatch is no verdict
        return Verdict("none", dispatch_id, "its dispatch record is missing")
    thread = dict_list(d.get("thread"))
    if not sha or not thread or sha not in str(thread[0].get("text") or ""):
        return Verdict("none", dispatch_id, "its dispatch is not a review of this sha")
    state = str(d.get("state") or "")
    if state in RUNNING_STATES:
        return Verdict("running", dispatch_id, state)
    summary = str((as_dict_or_none(d.get("result")) or {}).get("summary") or "")
    first = summary.strip().splitlines()[0].strip() if summary.strip() else ""
    if state == "done" and first == AGREE:
        return Verdict("agree", dispatch_id, first)
    if state == "done" and first == REVISE:
        return Verdict("revise", dispatch_id, first)
    return Verdict("none", dispatch_id, f"it ended {state or 'unknown'} without a verdict line")


def launch_warning(root: Path, *, feature: int | None, row: str | None) -> str:
    """A note for `dispatch new` when an implementation row launches on a feature with no agreed
    plan review. It warns and never refuses: Codex has no ExitPlanMode to
    gate, and an exempt plan leaves no record to find."""
    from .dispatch.state import load

    if row not in IMPLEMENTATION_ROWS or feature is None:
        return ""
    for rec in all_records(root):
        try:
            reviewed = load(root, str(rec.get("dispatch_id") or "")).get("feature")
        except Exception:  # noqa: BLE001, S112 — an unreadable review is not this feature's agreement
            continue
        if reviewed == feature and verdict(root, rec).state == "agree":
            return ""
    return (
        f"plan review: feature {feature:03d} has no agreed plan review on record. Run the `dispatch` "
        "action `plan-review` on the plan first, unless it says `review: small|none — <reason>`."
    )
