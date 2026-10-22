"""`plan-gate`, Claude PreToolUse on `ExitPlanMode`: a plan reaches the owner only after one blind
review round on its exact bytes found nothing blocking (`plan_review.py` holds the rule).

The plan shown is `tool_input.planFilePath` when that file reads, else `tool_input.plan`; its
hash is `plan_review.digest`, the same one the review's snapshot carries. It passes when

- one of its first 15 lines says `review: small — <reason>` or `review: none — <reason>`;
- the latest complete round on that sha has no finding its verifier kept as blocking (a round
  with only refuted or optional findings is clean), or that latest complete record is one from
  before rounds whose reviewers agreed; or
- `plan_review.ROUND_CAP` complete rounds of that plan file exist (each legacy record counts as
  one): the owner decides from there. The hook then adds a note with the ledger's verified
  blocking findings and disputes instead of a decision; it never answers `allow`, which would
  skip the owner's own approval prompt.

Otherwise it denies with the next step: a round advances only when the manager calls
`plan-review` again on the same plan after the supervisor reports a return. The hook only reads
files; it launches and writes nothing. An unmapped session is never a reason to deny. It fails
open on any error, like every hook. Codex has no approval tool and gets no plan hook: the old
`plan-gate-stop` read its chat for approval asks and blocked a trust question.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import plan_review
from ..records.narrow import as_dict_or_none
from .paths import deny

APPROVAL_TOOLS = frozenset({"ExitPlanMode"})
AGAIN = "call `plan-review` again on the same plan after the supervisor reports a return"


def plan_gate(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("tool_name") not in APPROVAL_TOOLS:
        return ""
    try:
        return _check(root, as_dict_or_none(payload.get("tool_input")) or {})
    except Exception:  # noqa: BLE001 — fail open
        return ""


def _presented(root: Path, data: dict[str, Any]) -> tuple[bytes, str] | None:
    """The plan's bytes and resolved path ("" when only the text came), or None when neither is
    there: Claude Code can send the text before a same-message Write lands (issue 96553)."""
    shown = str(data.get("planFilePath") or "")
    if shown:
        path = plan_review.resolve_plan(root, shown)
        try:
            return path.read_bytes(), str(path)
        except OSError:
            pass
    text = str(data.get("plan") or "")
    return (text.encode("utf-8"), "") if text.strip() else None


def _check(root: Path, data: dict[str, Any]) -> str:
    presented = _presented(root, data)
    if presented is None:
        return ""
    raw, path = presented
    if plan_review.exempt(raw.decode("utf-8", errors="replace")):
        return ""
    sha = plan_review.digest(raw)
    launch = f"call the `dispatch` tool with action `plan-review` and paths=[{path or '<the plan file>'}]"
    recs = plan_review.records(root, sha)
    if recs:
        done = plan_review.latest_complete(root, recs)
        if done is not None and plan_review.clean(root, done):
            return ""
        return _with_cap(root, path, sha, _where(root, recs[-1], launch))
    if path:
        older = [r for r in plan_review.records_for(root, path) if r.get("sha256") != sha]
        if older:
            last = older[-1]
            reason = (
                f"the plan changed since review {last.get('dispatch_id')} (sha256 "
                f"{str(last.get('sha256'))[:12]}, now {sha[:12]}); a round covers only the version it "
                f"read. {launch} for a new round."
            )
            return _with_cap(root, path, sha, reason)
    return deny(
        f"no blind review round of this plan yet (sha256 {sha[:12]}). {launch}; it launches two blind "
        f"reviewers. Then {AGAIN}, until a round completes with no verified blocking finding. A small "
        "plan can instead say `review: small — <reason>` in its first lines."
    )


def _where(root: Path, rec: dict[str, Any], launch: str) -> str:
    """Why the latest record of this sha does not open the gate, and the next step."""
    if not plan_review.is_round(rec):
        seen = plan_review.verdict(root, rec)
        if seen.state == "running":
            return f"plan review {seen.dispatch_id} of this exact plan is still running; {AGAIN}."
        if seen.state == "revise":
            return (
                f"plan review {seen.dispatch_id} asked for revision (`{plan_review.REVISE}`). Read its "
                f"blocking findings (`dispatch` action `show`), revise the plan, then {launch}."
            )
        return f"plan review {seen.dispatch_id} gave no verdict: {seen.detail}. {launch}."
    label = f"plan review round {plan_review.round_no(rec)} ({rec.get('dispatch_id')})"
    stalled = str(rec.get("stalled") or "")
    if stalled:
        return f"{label} cannot complete: {stalled}. Bring it to the owner."
    phase = rec.get("phase")
    if phase == "complete":
        return (
            f"{label} kept {', '.join(plan_review.blocking(rec))} as blocking after verification. Revise "
            "the plan and record each disposition (`plan-ledger`), or reject a finding with reasons; then "
            f"{launch} for a fresh round."
        )
    if phase == "verifying":
        verifier = as_dict_or_none(rec.get("verifier")) or {}
        return f"{label} is verifying (verifier {verifier.get('dispatch_id')}); {AGAIN}."
    return f"{label} of this exact plan is reviewing; {AGAIN}."


def _with_cap(root: Path, path: str, sha: str, reason: str) -> str:
    """Deny with `reason`, unless the plan file has had `ROUND_CAP` complete rounds: then the owner
    decides, and the hook only notes where the review stands and what the ledger holds."""
    rounds = plan_review.rounds_used(root, path) if path else 0
    if rounds < plan_review.ROUND_CAP:
        return deny(f"plan review: {reason}")
    note = (
        f"plan-gate: {rounds} complete review rounds of this plan file and no clean round on sha256 "
        f"{sha[:12]}; the round cap is reached, so the owner decides. Tell the owner where the review "
        f"stands: {reason} {_ledger(root, path)}"
    )
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}})


def _ledger(root: Path, path: str) -> str:
    from .. import plan_review_ledger as ledger_mod
    from ..core import AgentError

    try:
        lines = ledger_mod.owner_lines(ledger_mod.load(root, path))
    except AgentError as exc:
        return f"The findings ledger cannot be read: {exc}"
    if not lines:
        return "The ledger holds no verified blocking finding and no dispute."
    return "Ledger (verified blocking findings with their disposition, then disputes):\n" + "\n".join(
        f"- {line}" for line in lines
    )
