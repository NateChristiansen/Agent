"""Plan review in blind rounds: two fresh, read-only reviewers (Codex and Claude), then one
verifier, all on one frozen plan version.

This replaces manager pairing. Two long-lived managers agreeing to a plan's hash needed a
second live manager, a pair record on both sessions and a Stop-hook reading chat for approval
asks; that hook blocked a Codex manager's trust question to the owner. Now the manager calls the
`dispatch` tool's `plan-review` action: the harness reads the plan once, freezes the bytes as
`.agent/plan-reviews/<sha>.md` (read-only), embeds them in the work order, and records the round
as `<sha>-<round>.json`. Each later `plan-review` call on the same plan, made after the supervisor
reports a return, advances that round: `reviewing` (both reviewers' findings go to the ledger),
`verifying` (one verifier keeps or refutes each finding), `complete`. `plan-gate` lets
`ExitPlanMode` through when the latest complete round on the presented sha kept no finding as
blocking, for a plan that declares itself small, or after `ROUND_CAP` complete rounds of one plan
file, when the owner decides.

A record from before rounds is `<sha>.json` with one `dispatch_id` or two `reviewers`. It reads as
one round, complete once it has a verdict, clean when its reviewers agreed.

The hash is sha256 of the plan bytes with trailing newlines reduced to exactly one. ExitPlanMode's
`plan` text usually lacks the file's final newline, and an editor may add a second one; neither
changes what the owner reads. Nothing else is normalised, so any other edit voids a verdict.

This module only reads records and files: the hook imports it and must stay fast.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .core import agent_path
from .records.narrow import as_dict_or_none, dict_list

REVIEWS_DIR = agent_path(Path(), "plan-reviews")
ROUND_CAP = 3
AGREE = "VERDICT: agree"
REVISE = "VERDICT: revise"
VERDICT_FIELD = (
    "set the return's `verdict` field to `agree` or `revise`; the summary's wording is not read as one."
)
MARKER_LINES = 15
#: `review: small — <reason>` or `review: none: <reason>` near the top: the plan opts out, and
#: says why. A bare `review: small` with no reason does not count.
EXEMPT_RE = re.compile(r"^\s*review:\s*(small|none)\b\s*[—:-]?\s*\S", re.IGNORECASE)
RUNNING_STATES = frozenset({"queued", "running"})
#: Rubric rows that build or design; `dispatch new` warns when one launches unreviewed.
IMPLEMENTATION_ROWS = frozenset({"build", "design", "max"})
#: What a plan's blocking rules name (`blocking_rules`).
BAR = "the plan's own acceptance criteria"
ACCEPTANCE = "the plan's acceptance"


def exhaustive(what: str) -> str:
    """The instruction that asks a reviewer for every finding in `what`, not a sample."""
    return (
        f"Do not stop with a certain number of findings. Deeply review the entire {what} and bring back "
        "the ENTIRE list of findings, to avoid multiple revision rounds as best possible."
    )


def blocking_rules(bar: str, acceptance: str) -> str:
    """The two numbered rules every reviewer and verifier labels by. `bar` names where a threshold
    must be written to block; `acceptance` what a blocking failure is against."""
    return (
        f"1. A threshold or bar that is not in {bar} is optional, never blocking.\n"
        f"2. Blocking means a failure against {acceptance} on a path real use takes or plausibly would. "
        "A failure needing tampering, stacked faults, or input no one produces is optional unless you "
        "show it is likely."
    )


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


def round_path(root: Path, sha: str, round_no: int) -> Path:
    return reviews_dir(root) / f"{sha}-{round_no}.json"


def read_records(paths: list[Path]) -> list[dict[str, Any]]:
    """The readable JSON objects at `paths`, oldest launch first."""
    found: list[dict[str, Any]] = []
    for path in paths:
        try:
            data = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
        except OSError, ValueError:
            continue
        if data is not None:
            found.append(data)
    return sorted(found, key=_launch_order)


def _launch_order(rec: dict[str, Any]) -> tuple[float, int]:
    """The launch instant, so a stamp written after the local offset moved back still sorts
    later; a record with no readable stamp sorts first."""
    try:
        at = datetime.fromisoformat(str(rec.get("launched_at") or ""))
        instant = (at if at.tzinfo else at.astimezone()).timestamp()
    except ValueError, OverflowError, OSError:
        instant = float("-inf")
    return instant, round_no(rec)


def round_no(rec: dict[str, Any]) -> int:
    """The record's round number; 0 for a record from before rounds."""
    value = rec.get("round")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def records(root: Path, sha: str) -> list[dict[str, Any]]:
    """Every readable record of this sha, oldest launch first: the legacy `<sha>.json` and each
    round's `<sha>-<round>.json`."""
    base = reviews_dir(root)
    if not base.is_dir():
        return []
    return read_records([base / f"{sha}.json", *sorted(base.glob(f"{sha}-*.json"))])


def all_records(root: Path) -> list[dict[str, Any]]:
    """Every readable review record, oldest launch first."""
    base = reviews_dir(root)
    if not base.is_dir():
        return []
    return read_records(sorted(base.glob("*.json")))


def records_for(root: Path, plan_path: str) -> list[dict[str, Any]]:
    """Every review record of this resolved plan path, oldest launch first."""
    return [r for r in all_records(root) if r.get("plan_path") == plan_path]


def is_round(rec: dict[str, Any]) -> bool:
    """A round record has a `phase`; a record from before rounds has none."""
    return "phase" in rec


def blocking(rec: dict[str, Any]) -> list[str]:
    """The finding ids the verifier kept as blocking in a complete round."""
    raw = rec.get("blocking")
    return [str(i) for i in cast("list[object]", raw)] if isinstance(raw, list) else []


def complete(root: Path, rec: dict[str, Any]) -> bool:
    """A round is complete at phase `complete`; a legacy record once it has a verdict."""
    if is_round(rec):
        return rec.get("phase") == "complete"
    return verdict(root, rec).state in ("agree", "revise")


def clean(root: Path, rec: dict[str, Any]) -> bool:
    """A complete round that kept no finding as blocking, or a legacy record its reviewers agreed to."""
    if is_round(rec):
        return rec.get("phase") == "complete" and isinstance(rec.get("blocking"), list) and not blocking(rec)
    return verdict(root, rec).state == "agree"


def latest_complete(root: Path, recs: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((r for r in reversed(recs) if complete(root, r)), None)


def rounds_used(root: Path, plan_path: str) -> int:
    """What `ROUND_CAP` counts for a plan file: its complete rounds, and each legacy record as one."""
    return sum(1 for r in records_for(root, plan_path) if not is_round(r) or r.get("phase") == "complete")


@dataclass(frozen=True)
class Verdict:
    """Where a review stands: `agree`, `revise`, `running`, or `none` (ended without a verdict,
    or its dispatch is missing). `dispatch_id` names the reviewers the state comes from; `detail`
    is the verdict as read, or why there is none."""

    state: str
    dispatch_id: str
    detail: str = ""


def reviewer_ids(rec: dict[str, Any]) -> list[str]:
    """The record's reviewer dispatches: its `reviewers`, or the one `dispatch_id` of a record
    written before there were two."""
    if "reviewers" in rec:
        return [str(r.get("dispatch_id") or "") for r in dict_list(rec.get("reviewers"))]
    return [str(rec.get("dispatch_id") or "")]


def verdict(root: Path, rec: dict[str, Any]) -> Verdict:
    """Combine the reviewers' verdicts: `running` while any runs, `revise` if any revised, `agree`
    only if all agreed, else `none` naming the reviewer that gave no verdict."""
    sha = str(rec.get("sha256") or "")
    ids = reviewer_ids(rec)
    if not ids:
        return Verdict("none", "", "its record names no reviewer")
    seen = [reviewer_verdict(root, dispatch_id, sha, historical=not is_round(rec)) for dispatch_id in ids]
    if len(seen) == 1:
        return seen[0]
    for state in ("running", "revise"):
        hit = [v for v in seen if v.state == state]
        if hit:
            return Verdict(state, " and ".join(v.dispatch_id for v in hit), hit[0].detail)
    silent = [v for v in seen if v.state != "agree"]
    if not silent:
        return Verdict("agree", " and ".join(ids), seen[0].detail)
    who = {
        str(r.get("dispatch_id") or ""): f" ({r.get('provider')} {r.get('model')})"
        for r in dict_list(rec.get("reviewers"))
    }
    return Verdict(
        "none",
        " and ".join(v.dispatch_id for v in silent),
        "; ".join(f"reviewer {v.dispatch_id}{who.get(v.dispatch_id, '')}: {v.detail}" for v in silent),
    )


def reviewer_verdict(root: Path, dispatch_id: str, sha: str, *, historical: bool = False) -> Verdict:
    """Read the typed verdict, or a legacy summary line for a historical record. A dispatch whose
    work order does not carry the record's sha is not that review, whatever the record says."""
    from .dispatch.state import load

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
    result = as_dict_or_none(d.get("result")) or {}
    typed = result.get("verdict")
    if state == "done" and typed in ("agree", "revise"):
        return Verdict(str(typed), dispatch_id, str(typed))
    summary = str(result.get("summary") or "")
    first = summary.strip().splitlines()[0].strip() if summary.strip() else ""
    if historical and state == "done" and first in (AGREE, REVISE):
        return Verdict("agree" if first == AGREE else "revise", dispatch_id, first)
    return Verdict("none", dispatch_id, f"it ended {state or 'unknown'} without a typed verdict")


def launch_warning(root: Path, *, feature: int | None, row: str | None) -> str:
    """A note for `dispatch new` when an implementation row launches on a feature with no clean
    plan-review round (`clean`). It warns and never refuses: Codex has no ExitPlanMode to
    gate, and an exempt plan leaves no record to find."""
    from .dispatch.state import load

    if row not in IMPLEMENTATION_ROWS or feature is None:
        return ""
    for rec in all_records(root):
        try:
            reviewed = {load(root, dispatch_id).get("feature") for dispatch_id in reviewer_ids(rec)}
        except Exception:  # noqa: BLE001, S112 — an unreadable review is not this feature's agreement
            continue
        if reviewed == {feature} and clean(root, rec):
            return ""
    return (
        f"plan review: feature {feature:03d} has no agreed plan review on record: no review round "
        "finished with zero verified blocking findings. Run the `dispatch` action `plan-review` on the "
        "plan first, and call it again on the same plan after the supervisor reports each return, "
        "unless the plan says `review: small|none — <reason>`."
    )
