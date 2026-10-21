"""Dispatch operations: launch, resume, cancel, steer, read, clean up and collect workers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import dispatch as dispatch_mod
from .. import plan_review
from .. import repos as repos_mod
from ..core import AgentError, NotFound
from ..dispatch import machine, test_select
from ..dispatch.holdings_view import preview_lines, status_lines
from ..dispatch.state import load, shown_denials, verification_summary
from ..records.dispatch import git_of, result_of
from ..records.narrow import as_dict, as_dict_or_none, as_list, dict_list
from ._shared import Op
from .dispatch_preview import budget_line, pickup_lines, verification_preview_lines
from .dispatch_summary import activity, compact

# ---------------------------------------------------------------- dispatch


def dispatch_line(d: dict[str, Any]) -> str:
    """Summarize id, repo, state, branch, budget, attempt, model and effort for both clients.
    Sharing the summary prevents CLI and MCP drift (see ADR-0005); a `show` hint appears while
    the dispatch is running."""
    lines = [
        f"{d.get('id')} {d.get('short') or d.get('repo')} [{d.get('state')}] branch {d.get('branch')} "
        f"budget ${d.get('budget_usd')} attempt {d.get('attempt')}",
        f"  model: {d.get('model') or 'default'}  effort: {d.get('effort') or 'default'}",
    ]
    verification = verification_summary(d)
    lines.append(f"  recorded supervisor verification: {verification['status']}")
    lines += [f"    {reason}" for reason in verification["reasons"]]
    if verification["status"] == "exempt":
        lines.append("    exemption does not make worker permissions read-only")
    if d.get("verification_cleanup_pending") or d.get("cleanup_pending"):
        lines.append("  cleanup incomplete; repo lease retained — run dispatch collect")
    if d.get("state") == "running":
        lines.append(f"  show: dispatch tool, action show, dispatch_id {d.get('id')}")
    if d.get("work_order_url"):
        lines.append(f"  work order: `{d['work_order_url']}`")
    return "\n".join(lines)


def dispatch_preview_text(p: dict[str, Any]) -> str:
    """Render the same complete dry-run preview for CLI and MCP callers."""
    feature = (
        f"{int(p['feature']):03d} {p.get('feature_slug') or ''}".strip()
        if p.get("feature") is not None
        else "?"
    )
    lines = [f"dispatch preview — {p.get('repo')} for feature {feature}"]
    if p.get("work_order_url"):
        lines.append(f"  work order: `{p['work_order_url']}`")
    elif p.get("work_order_reason"):
        lines.append(f"  work order file unavailable: {p['work_order_reason']}")
    for key in (
        "goal",
        "escalated_from",
        # A forked escalation carries the old session context, so its preview names that session.
        "forked_from_session",
        "worktree",
        "branch",
        "base",
        "provider",
        "row",
        "model",
        "effort",
        "choice_reason",
        "budget_usd",
        # Show the explicit timeout override rather than the repository default.
        "timeout_minutes",
        "tasks",
        "notify",
        "mcp",
    ):
        if key in p and not (key in ("mcp", "escalated_from", "forked_from_session", "row") and not p[key]):
            # A reused worktree keeps its node_modules. Say so: the path
            # alone does not show whether the setup cost is about to be paid again.
            suffix = " (reused, setup already paid)" if key == "worktree" and p.get("reused") else ""
            if key == "effort" and p.get("effort_source"):
                suffix = f" ({p['effort_source']})"
            if key == "timeout_minutes" and p.get("timeout_source"):
                suffix = f" ({p['timeout_source']})"
            lines.append(budget_line(p) if key == "budget_usd" else f"  {key}: {p[key]}{suffix}")
    # A dry run reports current holdings without reserving anything. Repo slots enforce the cap; a
    # live legacy repo lease also refuses launch.
    lines += preview_lines(p)
    # Show the complete work order so the owner can approve the exact task and contract.
    if p.get("prompt"):
        note = f" ({p['prompt_bytes']} bytes, {p.get('prompt_lines')} lines)" if p.get("prompt_bytes") else ""
        rule = "  " + "─" * 68
        lines.append(f"  work order{note}:")
        lines.append(rule)
        lines += [f"    {line}" for line in str(p["prompt"]).splitlines()]
        lines.append(rule)
    options = p.get("options")
    if options:
        # The worker's transport, in the shape it actually launches with (see ADR-0009). `cli_path`
        # is here because it is the one thing a reader has to check at a glance: it must be the
        # `claude` on PATH, never the CLI bundled in claude-agent-sdk.
        lines.append("  options:")
        lines += [f"    {k}: {v}" for k, v in options.items() if v not in (None, [], 0)]
    findings = p.get("preflight")
    if findings is not None:
        # A row carrying a `warning` is not a command (the precedents check, the
        # per-commit claim): it is a sentence, not a verdict on a binary. They get their
        # own block, so the command table keeps meaning "what the rules say about each command".
        # Whether such a row refuses the launch is `check_preflight`'s call, not the split's:
        # precedents is advisory, the per-commit claim is a refusal, and both read the same here.
        commands = [f for f in findings if not f.get("warning")]
        warnings = [f for f in findings if f.get("warning")]
        lines.append(f"  preflight ({len(commands)} command(s) found):")
        for f in commands:
            lines.append(f"    {f['verdict']:<8} {f.get('rule') or '-'}  {f['command']}")
        blocked = sum(1 for f in commands if f["verdict"] != "allowed")
        lines.append(
            f"    {blocked} of {len(commands)} not allowed"
            if commands
            else "    nothing found; extraction is best-effort"
        )
        if warnings:
            lines.append("  warnings:")
            lines += [f"    {w['warning']}" for w in warnings]
    lines += verification_preview_lines(p.get("verification"))
    lines += pickup_lines(p)
    if p.get("work_order_url"):
        lines.append(f"Launch this order? `{p['work_order_url']}` (preview_id: {p.get('preview_id')})")
    return "\n".join(lines)


def dispatch_new(
    root: Path,
    *,
    repo: str,
    prompt: str,
    feature: str = "current",
    tasks: list[int] | list[str] | None = None,
    budget_usd: float | None = None,
    budget_reason: str | None = None,
    timeout_minutes: float | None = None,
    model: str | None = None,
    effort: str | None = None,
    choice_reason: str | None = None,
    just_go: bool = False,
    branch: str | None = None,
    base: str | None = None,
    target: str | None = None,
    dry_run: bool = False,
    preview_id: str | None = None,
    watch: bool = False,
    session_id: str | None = None,
    allow_unmatched: bool = False,
    mcp: list[str] | None = None,
    no_checks: bool = False,
    escalate_from: str | None = None,
    provider: str = "claude",
    row: str | None = None,
    writes: list[str] | None = None,
) -> Op:
    """Send a self-contained work order to a headless worker (see ADR-0006 and ADR-0008).

    `dry_run` writes the vault work order without reserving a worker slot and renders the complete
    preview for both front ends. `mcp` selects configured `worker_mcp` profiles. `no_checks`
    exempts report checks and supervisor verification without changing permissions. `escalate_from`
    reuses a stopped dispatch's repo, feature, branch and worktree a rubric tier up. `writes`
    follows `write_scope.reserve`.
    """
    # Recheck open tasks against main before the session launches its first dispatch.
    pickup = _first_dispatch_check(root, feature, session_id, escalate_from)
    result = dispatch_mod.new(
        root,
        repo=repo,
        feature=feature,
        prompt=prompt,
        tasks=[str(t) for t in (tasks or [])],
        budget=budget_usd,
        budget_reason=budget_reason,
        timeout_minutes=timeout_minutes,
        model=model,
        effort=effort,
        choice_reason=choice_reason,
        just_go=just_go,
        branch=branch,
        base=base,
        target=target,
        dry_run=dry_run,
        preview_id=preview_id,
        watch=watch,
        session_id=session_id,
        allow_unmatched=allow_unmatched,
        mcp=mcp,
        no_checks=no_checks,
        escalate_from=escalate_from,
        provider=provider,
        row=row,
        writes=writes,
        task_check=_record_summary(pickup),
    )
    # An unreviewed build or design launch is named, never refused.
    warning = plan_review.launch_warning(root, feature=result.get("feature"), row=result.get("row"))
    tail = f"\n  {warning}" if warning else ""
    # Directory claims conflict with every writer underneath them. Pickup checks are advisory.
    checks = {"writes_warnings": _broad_writes(root, repo, writes), "task_check": pickup}
    if dry_run:
        # Keep the result concise; the dispatch tool description provides launch guidance before
        # the call.
        preview = {**result, **checks}
        return Op({**preview, "plan_review_warning": warning}, dispatch_preview_text(preview) + tail)
    # The plan-review warning (a missing gate) reads before the advisory pickup lines.
    lines = "".join(f"\n{line}" for line in pickup_lines(checks))
    return Op(
        {**shown_record(result), **checks, "plan_review_warning": warning},
        dispatch_line(result) + tail + lines,
    )


def _broad_writes(root: Path, repo: str, writes: list[str] | None) -> list[str]:
    from ..dispatch import write_scope

    try:
        path = repos_mod.load_registry(root).require(repo).abs_path(root)
    except AgentError:
        return []
    return write_scope.broad_writes(path, writes)


def _first_dispatch_check(
    root: Path, feature: str, session_id: str | None, escalate_from: str | None
) -> dict[str, Any] | None:
    """`task_check` for the feature's first dispatch of this session, else None; run before the
    launch. An escalation follows an earlier dispatch, so it is never first. A caller with no
    session gets it every time. Read-only and advisory: a failure is `{"skipped": why}`, and a
    feature that does not resolve is left for `dispatch.new` to refuse."""
    from .. import features as features_mod
    from .. import sessions
    from .. import task_check as task_check_mod

    if escalate_from:
        return None
    try:
        resolved = features_mod.resolve(root, feature, session_id)
    except AgentError:
        return None
    try:
        sid: str | None = sessions.resolve_session_id(root, session_id)
    except AgentError:
        sid = None
    if sid and any(
        d.get("feature") == resolved.id and d.get("launched_by_session") == sid
        for d in dispatch_mod.load_all(root)
    ):
        return None
    try:
        return task_check_mod.check(root, resolved)
    except (AgentError, OSError, subprocess.SubprocessError) as exc:
        return {"skipped": str(exc)}


def _record_summary(checked: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the dispatch record keeps of the pickup check: counts and flagged task numbers."""
    if checked is None or "skipped" in checked:
        return checked
    tasks = dict_list(checked.get("tasks"))
    return {
        "open": checked.get("open"),
        "flagged": [t.get("index") for t in tasks if t.get("verdict") != "no signal"],
        "incomplete": checked.get("incomplete"),
    }


def dispatch_list(root: Path, *, feature: str | None = None, include_all: bool = False) -> Op:
    items = dispatch_mod.list_(root, feature=feature, all_=include_all)
    return Op(items, "\n".join(dispatch_line(d) for d in items) or "no dispatches")


def dispatch_show(root: Path, *, dispatch_id: str, consume: bool = True, caller: str | None = None) -> Op:
    """The full record. Clears `chat_pending` (the caller has now seen it), unless
    `consume=False`, `dispatch_read`'s read that writes nothing. `caller` is the reading session
    when the front end knows it (`state.show`).

    The text folds in the handoff path, the worker's own summary/questions/error, and the
    take-over line (`attach_hint`). The separate `dispatch_attach_info` tool/accessor is no
    longer needed (plan 2.2)."""
    try:
        d = dispatch_mod.show(root, dispatch_id=dispatch_id, consume=consume, caller=caller)
    except NotFound:
        # ADR-0036: a tagged id synced from another machine, or a former install, has a
        # handoff here but no local record. That is no local runtime, not an orphan.
        remote = machine.no_local_runtime(root, dispatch_id)
        if remote is None:
            raise
        writer = f"machine {remote['machine']}"
        if remote["this_install"]:
            writer = "this install, whose record is gone"
        text = (
            f"{remote['id']}: {remote['status']} on {writer}; no local runtime here. "
            f"Read {remote['handoff']}, or run dispatch show on that machine."
        )
        return Op(remote, text)
    d = {**shown_record(d), "verification_result": verification_summary(d)}
    lines = [dispatch_line(d), *status_lines(d.get("holds"))]
    for attempt, client in as_dict(d.get("client_versions")).items():
        client = as_dict(client)
        lines.append(
            f"  client (attempt {attempt}): {client.get('client') or 'unknown'} "
            f"{client.get('version') or '(version unknown)'}"
        )
    if seen := activity(root, d):
        lines.append(seen)
    if d.get("handoff"):
        lines.append(f"  handoff: {d['handoff']}")
    if d.get("forked_from_session"):
        # Name the forked session so the reader can see the context an escalation inherits.
        lines.append(f"  forked from: {d.get('escalated_from') or '?'} (session {d['forked_from_session']})")
    res = result_of(d)
    if summary := res.get("summary"):
        lines.append(f"  worker-reported {res.get('status')}: {summary}")
    for q in res.get("open_questions") or []:
        lines.append(f"  question: {q}")
    if (git := git_of(d)) is not None:
        lines.append(f"  git: {len(git['commits'])} commit(s), {len(git['files'])} file(s) since base")
    # The record has accumulated these across attempts, and
    # nothing has ever printed them, so the denial count a retro asks for had to be dug out of
    # the json by hand. A denial is a harness fault: name the commands, not just the number.
    denials = dispatch_mod.permission_denials(d)
    if denials:
        lines.append(f"  permission denials: {len(denials)}")
        lines += [f"    {c}" for c in denials]
    # What the `can_use_tool` callback actually saw and answered, recorded while the worker
    # was still inside the call. The list above is the after-the-fact cross-check. These two
    # are never merged, and a count that disagrees is the interesting reading.
    events = dispatch_mod.permission_events(d)
    if events:
        denied = dispatch_mod.denied_permission_events(d)
        lines.append(f"  permission events: {len(events)} (denied {len(denied)})")
        lines += [f"    {e.get('input')}: {e.get('reason')}" for e in denied]
    if d.get("error"):
        lines.append(f"  error: {d['error']}")
    if d.get("session_id"):
        lines.append(f"  take over: {dispatch_mod.attach_hint(root, dispatch_id=dispatch_id)}")
    return Op(d, "\n".join(lines))


def dispatch_read_show(root: Path, *, dispatch_id: str, detail: bool = False) -> Op:
    """`dispatch_read` show: writes nothing, consumes nothing. By default a
    concise summary (state, verdict line, verification, whether the verdict is read, evidence
    links); `detail=True` is `dispatch_show`'s full record and text, still without consuming."""
    from ..dispatch import verdicts

    op = dispatch_show(root, dispatch_id=dispatch_id, consume=False)
    data = as_dict_or_none(op.data)
    if detail or data is None or "state" not in data or "attempt" not in data:
        return op  # full detail, or a synced id with no local record
    res = result_of(data)
    verdict = (
        f"worker-reported {res.get('status')}: {res.get('summary')}"
        if res.get("summary")
        else (f"error: {data['error']}" if data.get("error") else None)
    )
    verification = as_dict_or_none(data.get("verification_result")) or {}
    questions = [str(q) for q in as_list(res.get("open_questions"))]
    summary: dict[str, Any] = {
        "id": data.get("id"),
        "feature": data.get("feature"),
        "repo": data.get("short"),
        "state": data.get("state"),
        "attempt": data.get("attempt"),
        "verdict": verdict,
        "questions": questions,
        "verification": verification.get("status"),
        "verdict_read": verdicts.is_read(data),
        "unresolved_branch": verdicts.unresolved_branch(data),
        "evidence": {
            "handoff": data.get("handoff"),
            "branch": data.get("branch"),
            "worktree": data.get("worktree"),
        },
    }
    lines = [
        f"{summary['id']} {summary['repo']} [{summary['state']}] attempt {summary['attempt']}; "
        f"verification {summary['verification']}; verdict {'read' if summary['verdict_read'] else 'unread'}"
    ]
    if verdict:
        lines.append(f"  {verdict}")
    lines += [f"  question: {q}" for q in questions]
    if data.get("handoff"):
        lines.append(f"  handoff: {data['handoff']}")
    lines.append(
        "  detail=true for the full record; dispatch consume marks the verdict read"
        if not summary["verdict_read"]
        else "  detail=true for the full record"
    )
    return Op(summary, "\n".join(lines))


def dispatch_consume(root: Path, *, dispatch_id: str, session_id: str | None) -> Op:
    """Mark the current verdict read for the calling manager: the generation-bound
    `verdict_read` record, then the same notice consumption `show` did. Refuses a caller that
    is neither the launcher nor, once it has ended, a live manager mapped to the feature."""
    from ..dispatch import notice_producers, verdicts

    entry = verdicts.record_read(root, dispatch_id, session_id, "consume", strict=True)
    notice_producers.consume_read(root, dispatch_id, session_id)
    d = load(root, dispatch_id)
    mark = entry or {}
    text = (
        f"{dispatch_id}: verdict read (attempt {mark.get('attempt')}, generation {mark.get('generation')}) "
        f"by {mark.get('by')}"
    )
    return Op(shown_record(d), text)


def shown_record(d: dict[str, Any]) -> dict[str, Any]:
    """A copy of a dispatch record for an Op to return. One written before claude's denials were
    stored shown still holds raw ones, on the record, its `last_result` and its `pending_outcome`.
    Either of those two that is not a dict is no envelope and is returned as None."""
    d = dict(d)
    for key in ("last_result", "pending_outcome"):
        held = as_dict_or_none(d.get(key))
        if key in d and held is None:
            d[key] = None
        elif held is not None and "permission_denials" in held:
            d[key] = {**held, "permission_denials": shown_denials(held["permission_denials"])}
    if "permission_denials" in d:
        d["permission_denials"] = shown_denials(d["permission_denials"])
    return d


def dispatch_cancel(root: Path, *, dispatch_id: str) -> Op:
    d = shown_record(dispatch_mod.cancel(root, dispatch_id=dispatch_id))
    return Op(d, f"cancelled {d['id']}; worktree kept at {d.get('worktree')}")


def dispatch_abandon(root: Path, *, dispatch_id: str, note: str, by: str = "owner") -> Op:
    d = shown_record(dispatch_mod.abandon(root, dispatch_id=dispatch_id, note=note, by=by))
    return Op(d, f"abandoned {d['id']} (was {d['abandoned_from']}); worktree kept at {d.get('worktree')}")


def dispatch_open(root: Path, *, dispatch_id: str, print_only: bool = False) -> Op:
    result = dispatch_mod.open_(root, dispatch_id=dispatch_id, print_only=print_only)
    if result["opened"]:
        text = f"opened {result['id']} ({result['mode']}) in an Obsidian terminal"
    else:
        if sys.platform == "win32":
            quoted = str(result["cwd"]).replace("'", "''")
            text = f"Set-Location -LiteralPath '{quoted}'; {result['command']}  # {result.get('reason')}"
        else:
            text = f"cd {result['cwd']} && {result['command']}  ({result.get('reason')})"
    return Op(result, text)


def dispatch_ack(root: Path, *, dispatch_id: str, note: str | None = None, by: str = "owner") -> Op:
    d = shown_record(dispatch_mod.ack(root, dispatch_id=dispatch_id, note=note, by=by))
    return Op(d, f"acknowledged {d['id']} ({d['state']}); off the Workers view until it runs again")


def dispatch_say(root: Path, *, dispatch_id: str, message: str) -> Op:
    d = shown_record(dispatch_mod.say(root, dispatch_id=dispatch_id, message=message))
    # Under the SDK a steer lands inside the running turn (see ADR-0009), not at the next boundary.
    queued = "queued behind an unread message" if d["pending"] else "delivered into the worker's running turn"
    return Op(d, f"said to {d['id']}: {queued}")


def dispatch_collect(root: Path, *, dispatch_id: str | None = None, wait: float = 0.0) -> Op:
    """Ingest finished workers into their handoffs and the feature log.

    With no id, collects every dispatch waiting. `wait` is how long to hold for a supervisor
    still finishing."""
    items = dispatch_mod.collect(root, dispatch_id=dispatch_id, wait=wait)
    return Op(items, "\n".join(dispatch_line(d) for d in items) or "(nothing to collect)")


def dispatch_cleanup(
    root: Path,
    *,
    feature: str | None = None,
    dispatch_id: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    delete_branches: bool = False,
) -> Op:
    """Counts plus up to ten examples per list; past that, the whole result is in `evidence`."""
    from .. import tidy
    from ..dispatch import worktrees

    full = dispatch_mod.cleanup(root, feature=feature, dispatch_id=dispatch_id, force=force, dry_run=dry_run)
    if delete_branches:
        full = worktrees.delete_branches(root, full)
    result = tidy.report(root, "dispatch-cleanup", full, tuple(k for k in CLEANUP_LISTS if k in full))
    counts = result["counts"]

    def listed(key: str, label: str) -> str:
        rows = [_cleanup_row(r) for r in as_list(result.get(key))]
        more = counts[key] - len(rows)
        return f"{label} ({counts[key]}): {', '.join(rows) or 'none'}" + (
            f", {more} more" if more > 0 else ""
        )

    lines = [listed("would_remove", "dry run: would remove") if dry_run else listed("removed", "removed")]
    for key in ("skipped", "blocked", "missing", "branches_deleted", "branches_skipped"):
        if result.get(key):
            lines.append(listed(key, key.replace("_", " ")))
    if result.get("evidence"):
        lines.append(f"full result: {result['evidence']}")
    return Op(result, "\n".join(lines))


CLEANUP_LISTS = (
    "would_remove",
    "removed",
    "skipped",
    "blocked",
    "missing",
    "branches_kept",
    "branches_deleted",
    "branches_skipped",
)


def _cleanup_row(row: object) -> str:
    entry = as_dict_or_none(row)
    if entry is None:
        return str(row)
    where = entry.get("worktree") or entry.get("branch")
    return f"{where} [{entry.get('id') or ''}] ({entry.get('reason') or entry.get('state')})"


def dispatch_reconcile(
    root: Path, *, dispatch_id: str | None = None, path: str | None = None, apply: bool = False
) -> Op:
    from ..dispatch.worktrees import reconcile

    r = reconcile(root, dispatch_id=dispatch_id, path=path, apply=apply)
    if r["holds"]:
        text = f"{r['path']} ({r['state']}) kept registered: " + "; ".join(r["holds"])
    elif r["already"]:
        text = f"{r['path']} is missing and no longer registered; nothing to do"
    elif r["reconciled"]:
        text = f"reconciled {r['path']}: its registration is dropped; records marked {r['records_marked']}"
    else:
        text = (
            f"preview: {r['path']} is missing; apply drops its registration (HEAD {r['head']} is preserved)"
        )
    return Op(r, text)


def dispatch_restore(root: Path, *, dispatch_id: str, head: str | None = None, apply: bool = False) -> Op:
    from ..dispatch.worktrees import restore

    r = restore(root, dispatch_id=dispatch_id, head=head, apply=apply)
    if r.get("already"):
        first = f"{r['path']} already present at {r['head']}"
    elif r["restored"]:
        first = f"restored {r['path']} on {r['branch']} at {r['head']}"
    else:
        first = f"{'kept' if r['holds'] else 'preview'}: {r['path']} on {r['branch']} at {r['head'] or '?'}"
    lines = [
        first,
        *(f"  hold: {h}" for h in r["holds"]),
        *(f"  warning: {w}" for w in as_list(r.get("warnings"))),
    ]
    return Op(r, "\n".join(lines))


def dispatch_discard_files(
    root: Path,
    *,
    dispatch_id: str,
    paths: list[str],
    plan_hashes: dict[str, str] | None = None,
    apply: bool = False,
) -> Op:
    from ..dispatch.worktrees import discard_files

    r = discard_files(root, dispatch_id=dispatch_id, paths=paths, plan_hashes=plan_hashes, apply=apply)
    if r["dry_run"]:
        lines = [f"preview: would delete {len(r['plan_hashes'])} file(s) from {r['worktree']}:"]
        lines += [f"  {name}  sha256 {digest}" for name, digest in r["plan_hashes"].items()]
        lines.append("only after the owner agrees: apply=True with plan_hashes set to this mapping")
        return Op(r, "\n".join(lines))
    return Op(r, f"discarded from {r['worktree']}: " + ", ".join(r["discarded"]))


def dispatch_reverify(root: Path, *, dispatch_id: str) -> Op:
    """Rerun only the supervisor checks on a terminal dispatch's recorded head."""
    from ..dispatch.reverify import reverify

    row = reverify(root, dispatch_id)
    result = row["result"]
    tree = row["worker_tree"]
    dirt = (
        "worker worktree not observed"
        if not tree.get("observed")
        else "worker worktree clean"
        if tree.get("tracked_clean") and tree.get("index_clean")
        else "worker worktree has uncommitted changes (unverified, excluded)"
    )
    lines = [
        f"{dispatch_id} reverify {result['status']} at {row['final_sha'][:12]} in a clean checkout",
        f"  {dirt}",
        "  dispatch state unchanged; this is evidence for the manager",
    ]
    lines += [f"  reason: {reason}" for reason in as_list(result.get("reasons"))]
    if row.get("cleanup_pending"):
        lines.append(f"  checkout cleanup failed; repo lease retained: {row['checkout']}")
    return Op(row, "\n".join(lines))


def dispatch_collect_files(root: Path, *, dispatch_id: str, paths: list[str]) -> Op:
    result = dispatch_mod.collect_files(root, dispatch_id=dispatch_id, paths=paths)
    return Op(result, "collected: " + ", ".join(result["artifacts"]))


DISPATCH_WATCH_LINES = 40


def dispatch_watch(root: Path, *, dispatch_id: str, lines: int = DISPATCH_WATCH_LINES) -> Op:
    """A bounded tail for a tool call, unlike the CLI's `--follow`.

    Returns the last `lines` digest lines only, never the full transcript. A manager session
    can afford to check a running worker without pulling its whole history into context."""
    all_lines = list(dispatch_mod.watch(root, dispatch_id=dispatch_id, follow=False, digest=True))
    tail = all_lines[-lines:] if lines > 0 else []
    # The bounded liveness view a manager polls: compact status and the last sign of life.
    d = load(root, dispatch_id)
    seen = activity(root, d)
    data = {
        "id": dispatch_id,
        "status": compact(d),
        "activity": seen.strip(),
        "lines": tail,
        "truncated": len(all_lines) > len(tail),
    }
    return Op(data, "\n".join([dispatch_line(d), *([seen] if seen else []), *tail]))


def dispatch_ledger(root: Path, *, by: str = "pr", refresh: bool = False, repo: str | None = None) -> Op:
    """The deterministic outcome ledger: finished dispatches grouped by PR, feature or
    week.

    `refresh=True` force-refreshes every dispatch record with a branch (scoped to `repo` when
    given, a registry name or short) against `gh`, then runs the stacked-PR inheritance pass,
    before reading. Without it, nothing calls `gh`, and the ledger reads whatever PR data is
    already on the records. A `gh` "failure" here is detected the only way it can be from
    outside `refresh_pr` (which never raises): a forced refresh that left an existing non-null
    `pr` value byte-for-byte unchanged, including its `checked_at`, did not actually reach
    GitHub. A record that had no PR before and still has none after a failed call is
    indistinguishable from one that was genuinely re-checked and still has none, so that case
    is not reported as a failure."""
    lines: list[str] = []
    if refresh:
        registry = repos_mod.load_registry(root)
        target = registry.require(repo) if repo else None
        refreshed = 0
        failures: list[str] = []
        for d in dispatch_mod.load_all(root):
            if not d.get("branch"):
                continue
            if target is not None and d.get("repo") != target.name:
                continue
            rp = registry.find(d.get("repo") or "")
            if rp is None:
                failures.append(f"{d['id']}: repo {d.get('repo')!r} is not in the registry")
                continue
            before = json.dumps(d.get("pr"), sort_keys=True, default=str)
            pr = dispatch_mod.refresh_pr(root, d, rp, force=True)
            refreshed += 1
            after = json.dumps(pr, sort_keys=True, default=str)
            if before == after and d.get("pr") is not None:
                failures.append(f"{d['id']}: gh did not return a fresh result")
        dispatch_mod.apply_pr_inheritance(root, dispatch_mod.load_all(root))
        summary = f"refreshed {refreshed} record(s)"
        if failures:
            summary += f"; {len(failures)} gh failure(s): " + "; ".join(failures)
        lines.append(summary)
    else:
        lines.append("no refresh; reading the recorded PR data")
    rows = dispatch_mod.ledger_rows(root, by=by)
    lines += [dispatch_mod.render_ledger(rows, by), test_select.rates(dispatch_mod.load_all(root))]
    return Op(rows, "\n".join(lines))
