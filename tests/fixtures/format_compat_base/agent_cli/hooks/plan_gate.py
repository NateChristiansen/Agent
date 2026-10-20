# ruff: noqa
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .. import plan_review
from ..records.narrow import as_dict_or_none
from .paths import deny

APPROVAL_TOOLS = frozenset({"ExitPlanMode"})


def plan_gate(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("tool_name") not in APPROVAL_TOOLS:
        return ""
    try:
        return _check(root, as_dict_or_none(payload.get("tool_input")) or {})
    except Exception:
        return ""


def _presented(root: Path, data: dict[str, Any]) -> tuple[bytes, str] | None:
    shown = str(data.get("planFilePath") or "")
    if shown:
        path = plan_review.resolve_plan(root, shown)
        try:
            return (path.read_bytes(), str(path))
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
    launch = f"launch the `dispatch` tool with action `plan-review` and paths=[{path or '<the plan file>'}]"
    rec = plan_review.record(root, sha)
    if rec is not None:
        seen = plan_review.verdict(root, rec)
        if seen.state == "agree":
            return ""
        if seen.state == "running":
            reason = (
                f"plan review {seen.dispatch_id} of this exact plan is still running; wait for its verdict."
            )
        elif seen.state == "revise":
            reason = f"plan review {seen.dispatch_id} asked for revision (`{plan_review.REVISE}`). Read its blocking findings (`dispatch` action `show`), revise the plan, then {launch}."
        else:
            reason = f"plan review {seen.dispatch_id} gave no verdict: {seen.detail}. {launch}."
        return _with_cap(root, path, sha, reason)
    if path:
        older = [r for r in plan_review.records_for(root, path) if r.get("sha256") != sha]
        if older:
            last = older[-1]
            reason = f"the plan changed since review {last.get('dispatch_id')} (sha256 {str(last.get('sha256'))[:12]}, now {sha[:12]}); a verdict covers only the version it read. {launch}."
            return _with_cap(root, path, sha, reason)
    return deny(
        f"no cross-client review of this plan yet (sha256 {sha[:12]}). {launch}, wait for `{plan_review.AGREE}`, then present it. A small plan can instead say `review: small — <reason>` in its first lines."
    )


def _with_cap(root: Path, path: str, sha: str, reason: str) -> str:
    rounds = len(plan_review.records_for(root, path)) if path else 0
    if rounds < plan_review.ROUND_CAP:
        return deny(f"plan review: {reason}")
    note = f"plan-gate: {rounds} reviews of this plan file and no agreement on sha256 {sha[:12]}; the round cap is reached, so the owner decides. Tell the owner the last review's state: {reason}"
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}})
