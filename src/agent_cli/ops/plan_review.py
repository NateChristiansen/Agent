"""`dispatch` action `plan-review`: launch two blind, read-only reviewers of a plan, Codex and
Claude, on the same work order (`plan_review.py` holds the rule and the hash).

The plan is read once. Those bytes are hashed, frozen as `.agent/plan-reviews/<sha>.md` (mode
0444), and embedded in the work order, so both reviewers judge exactly the version the gate later
checks, never the live file (an edit after launch changes neither the sha nor the text). Neither
reviewer sees the other's findings. The launching session's client does not pick them.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .. import dispatch as dispatch_mod
from .. import plan_review, sessions
from ..core import AgentError, atomic_write, iso, now
from ._shared import Op

ROW = "review"
#: Provider, model and effort of each blind reviewer. Passed explicitly, so a repo
#: `dispatch.model` pin cannot replace them.
REVIEWERS: tuple[tuple[str, str, str], ...] = (
    ("codex", "gpt-6.1-sol", "high"),
    ("claude", "sonnet", "high"),
)


def _write_snapshot(path: Path, body: bytes) -> None:
    """Write the frozen plan read-only. One that exists already holds these exact bytes (its
    name is their hash), and a read-only file cannot be replaced on Windows, so it is kept."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def work_order(sha: str, text: str) -> str:
    """The reviewer's whole input: the contract, then the frozen plan between sha-stamped marks."""
    return (
        f"Plan review of sha256 {sha}: a fresh, read-only review of a plan before the owner sees it.\n\n"
        "Review only the plan snapshot below. It is frozen: do not open the plan's live file, which may "
        "have changed since. Change no file; this dispatch is read-only.\n\n"
        "Do not stop with a certain number of findings. Deeply review the entire plan and bring back the "
        "ENTIRE list of findings, to avoid multiple revision rounds as best possible.\n\n"
        "Blocking rules:\n"
        "1. A threshold or bar that is not in the plan's own acceptance criteria is optional, never "
        "blocking.\n"
        "2. Blocking means a failure against the plan's acceptance on a path real use takes or "
        "plausibly would. A failure needing tampering, stacked faults, or input no one produces is "
        "optional unless you show it is likely.\n\n"
        f"Reply: the first line of your returned summary is exactly `{plan_review.AGREE}` or "
        f"`{plan_review.REVISE}`. Revise only for blocking findings. List each blocking finding under "
        "`Blocking:`, and label every other point `Optional:`. Each finding states its path and the "
        "read or probe behind it. Put the whole review in your returned summary.\n\n"
        f"----- BEGIN PLAN SNAPSHOT sha256 {sha} -----\n"
        f"{text.rstrip(chr(10))}\n"
        f"----- END PLAN SNAPSHOT sha256 {sha} -----\n"
    )


def dispatch_plan_review(root: Path, *, paths: list[str], session_id: str | None = None) -> Op:
    """Launch the two blind reviews of the plan at `paths[0]` (`REVIEWERS`)."""
    if len(paths) != 1:
        raise AgentError("plan-review takes exactly one plan path in `paths`")
    sid = sessions.resolve_session_id(root, session_id)
    plan = plan_review.resolve_plan(root, paths[0])
    try:
        raw = plan.read_bytes()  # the one read: everything below uses these bytes
    except OSError as exc:
        raise AgentError(f"cannot read the plan {plan}: {exc}") from exc
    body = plan_review.normalise(raw)
    sha = plan_review.digest(raw)
    prior = plan_review.record(root, sha)
    if prior is not None:
        seen = plan_review.verdict(root, prior)
        if seen.state == "agree":
            return Op(prior, f"review {seen.dispatch_id} already agreed to this exact plan (sha256 {sha}).")
        if seen.state in ("running", "revise"):
            what = "is still running" if seen.state == "running" else "asked for revision"
            raise AgentError(
                f"review {seen.dispatch_id} of this exact plan {what}; "
                + ("wait for it." if seen.state == "running" else "revise the plan, then launch again.")
            )
    from ..verify_record import harness_repo

    repo = harness_repo(root)
    snapshot = plan_review.reviews_dir(root) / f"{sha}.md"
    _write_snapshot(snapshot, body)
    order = work_order(sha, body.decode("utf-8", errors="replace"))
    reviewers: list[dict[str, Any]] = []
    states: dict[str, Any] = {}
    for provider, model, effort in REVIEWERS:
        try:
            result = dispatch_mod.new(
                root,
                repo=str(repo.name),
                feature="current",
                prompt=order,
                provider=provider,
                row=ROW,
                model=model,
                effort=effort,
                # The plan travels in the work order, so landed main is the right checkout.
                base=repo.default_branch,
                writes=[],
                no_checks=True,
                # The owner's standing rule launches this review without a per-launch preview.
                just_go=True,
                # The plan's own fences are data under review, not commands the reviewer runs.
                allow_unmatched=True,
                choice_reason="plan review: one of two blind reviewers, read-only",
                session_id=sid,
            )
        except AgentError as exc:
            if not reviewers:
                raise
            launched = ", ".join(r["dispatch_id"] for r in reviewers)
            raise AgentError(
                f"plan review launched {launched} but not its {provider} reviewer: {exc}. "
                f"Cancel {launched}, then launch the plan review again."
            ) from exc
        reviewers.append(
            {"dispatch_id": result["id"], "provider": provider, "model": model, "effort": effort}
        )
        states[result["id"]] = result.get("state")
    ids = [r["dispatch_id"] for r in reviewers]
    rec: dict[str, Any] = {
        "sha256": sha,
        "plan_path": str(plan),
        "snapshot": str(snapshot),
        # A label for messages that name "the review"; `reviewers` is what the verdict reads.
        "dispatch_id": " and ".join(ids),
        "reviewers": reviewers,
        "session_id": sid,
        "launched_at": iso(now()),
    }
    atomic_write(
        plan_review.reviews_dir(root) / f"{sha}.json", json.dumps(rec, indent=2, sort_keys=True) + "\n"
    )
    names = " and ".join(f"{r['dispatch_id']} ({r['provider']} {r['model']})" for r in reviewers)
    return Op(
        {**rec, "states": states},
        f"plan review launched two blind reviewers, {names}, for {plan.name} (sha256 {sha[:12]}). "
        f"Present the plan once both summaries start `{plan_review.AGREE}`.",
    )
