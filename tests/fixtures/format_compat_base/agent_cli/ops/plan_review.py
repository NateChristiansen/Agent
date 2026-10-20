# ruff: noqa
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


def _other_client(provider: str) -> str:
    return "claude" if provider == "codex" else "codex"


def _write_snapshot(path: Path, body: bytes) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.chmod(tmp, 292)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def work_order(sha: str, text: str) -> str:
    return f"Plan review of sha256 {sha}: one fresh, read-only review of a plan before the owner sees it.\n\nReview only the plan snapshot below. It is frozen: do not open the plan's live file, which may have changed since. Change no file; this dispatch is read-only. Read the harness repo only to check a claim the plan makes, and prefer an executed read-only probe to an opinion.\n\nReview contract:\n1. For each step, does it serve the plan's Context? Name any step that does not.\n2. Does a client (Claude Code, Codex) or a vendor already provide what a step builds? Name it.\n3. Correctness risks: what would break, regress, or be impossible to verify as written.\n4. A threshold or bar that is not in the plan's own acceptance criteria is optional, never blocking.\n5. Blocking means a failure against the plan's acceptance on a path real use takes or plausibly would. A failure needing tampering, stacked faults, or input no one produces is optional unless you show it is likely. Each finding states its path.\n\nReply: the first line of your returned summary is exactly `{plan_review.AGREE}` or `{plan_review.REVISE}`. Revise only for blocking findings. List each blocking finding under `Blocking:`, and label every other point `Optional:`.\n\n----- BEGIN PLAN SNAPSHOT sha256 {sha} -----\n{text.rstrip(chr(10))}\n----- END PLAN SNAPSHOT sha256 {sha} -----\n"


def dispatch_plan_review(root: Path, *, paths: list[str], session_id: str | None = None) -> Op:
    if len(paths) != 1:
        raise AgentError("plan-review takes exactly one plan path in `paths`")
    sid = sessions.resolve_session_id(root, session_id)
    plan = plan_review.resolve_plan(root, paths[0])
    try:
        raw = plan.read_bytes()
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
    provider = _other_client(str((sessions.get(root, sid) or {}).get("provider") or "claude"))
    snapshot = plan_review.reviews_dir(root) / f"{sha}.md"
    _write_snapshot(snapshot, body)
    result = dispatch_mod.new(
        root,
        repo=str(repo.name),
        feature="current",
        prompt=work_order(sha, body.decode("utf-8", errors="replace")),
        provider=provider,
        row=ROW,
        writes=[],
        no_checks=True,
        just_go=True,
        allow_unmatched=True,
        choice_reason="plan review: one fresh reviewer on the other client, read-only",
        session_id=sid,
    )
    rec: dict[str, Any] = {
        "sha256": sha,
        "plan_path": str(plan),
        "snapshot": str(snapshot),
        "dispatch_id": result["id"],
        "session_id": sid,
        "provider": provider,
        "launched_at": iso(now()),
    }
    atomic_write(
        plan_review.reviews_dir(root) / f"{sha}.json", json.dumps(rec, indent=2, sort_keys=True) + "\n"
    )
    return Op(
        {**rec, "state": result.get("state")},
        f"plan review {result['id']} launched on {provider} for {plan.name} (sha256 {sha[:12]}). Present the plan once its summary starts `{plan_review.AGREE}`.",
    )
