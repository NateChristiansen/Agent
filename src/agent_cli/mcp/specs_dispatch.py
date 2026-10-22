"""Expose dispatch mutations and worker logging as typed tools. Pure `dispatch_read` actions use a
separate tool so polling requires no write permission and consumes no notices or verdicts.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, Literal

from .. import ops
from ..core import AgentError
from ..ops.dispatch_summary import compact
from ..records.narrow import as_dict_or_none
from .tools import (
    WORKER_KINDS,
    Ctx,
    DispatchAction,
    DispatchLedgerBy,
    DispatchReadAction,
    Op,
    PlanDisposition,
    ToolSpec,
    WorkerLogKind,
    dispatch_require,
)

# ------------------------------------------------------------------ dispatch (one tool, action-routed)

#: Actions that change one dispatch and returned its whole record (`ops.dispatch_summary`).
COMPACT_ACTIONS = frozenset({"new", "resume", "cancel", "abandon", "ack", "say", "consume"})


def _compact_results(fn: Callable[..., Op]) -> Callable[..., Op]:
    """Return `compact` records for `COMPACT_ACTIONS` unless `verbose` or a dry run. The tool's
    schema is `fn`'s signature (`functools.wraps`)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kw: Any) -> Op:
        op = fn(*args, **kw)
        action = kw.get("action", args[1] if len(args) > 1 else None)
        data = as_dict_or_none(op.data)
        if action in COMPACT_ACTIONS and not kw.get("verbose") and not kw.get("dry_run") and data is not None:
            return Op(compact(data), op.text)
        return op

    return wrapper


def _legacy_read(ctx: Ctx, dispatch_id: str, session_id: str | None, shown: dict[str, Any]) -> None:
    """The `show` alias's verdict-read, for a caller allowed to write one, bound to the attempt
    and generation `shown` returned: a newer attempt or notice that landed after the show is not
    what the caller read, so it stays unread. Never fails the show: the
    record it returned stands whether or not the read could be recorded."""
    from ..dispatch import verdicts

    try:
        verdicts.record_read(
            ctx.root, dispatch_id, ctx.session_id(session_id), "show", expected=verdicts.key(shown)
        )
    except (AgentError, OSError) as exc:
        import sys

        sys.stderr.write(f"note: verdict-read not recorded for {dispatch_id} ({exc})\n")


@_compact_results
def dispatch(
    ctx: Ctx,
    action: DispatchAction,
    dispatch_id: str | None = None,
    repo: str | None = None,
    prompt: str | None = None,
    feature: str | None = None,
    tasks: list[int] | None = None,
    budget_usd: float | None = None,
    budget_reason: str | None = None,
    timeout_minutes: float | None = None,
    model: str | None = None,
    effort: str | None = None,
    choice_reason: str | None = None,
    provider: Literal["claude", "codex"] = "claude",
    row: str | None = None,
    just_go: bool = False,
    dry_run: bool = False,
    preview_id: str | None = None,
    allow_unmatched: bool = False,
    mcp: list[str] | None = None,
    branch: str | None = None,
    base: str | None = None,
    target: str | None = None,
    answer: str | None = None,
    message: str | None = None,
    force: bool = False,
    wait: float = 0.0,
    paths: list[str] | None = None,
    lines: int = ops.DISPATCH_WATCH_LINES,
    include_all: bool = False,
    session_id: str | None = None,
    by: DispatchLedgerBy = "pr",
    refresh: bool = False,
    no_checks: bool = False,
    escalate_from: str | None = None,
    again: bool = False,
    writes: list[str] | None = None,
    verbose: bool = False,
    apply: bool = False,
    head: str | None = None,
    delete_branches: bool = False,
    plan_hashes: dict[str, str] | None = None,
    compare: str | None = None,
    pr: str | None = None,
    finding_id: str | None = None,
    disposition: PlanDisposition | None = None,
    reraise_of: str | None = None,
) -> Op:
    """One tool over `ops.dispatch_*` (plan 2.2). `action` picks the operation. The rest of
    the parameters are the union of what every action needs, and are optional except the ones
    a given action requires. That is checked below, not at the schema level, so one tool can
    serve all ten without ten overlapping required-parameter sets.

    `branch`/`base` and the `collect` action are here because the CLI had them all along and
    this tool did not. Without them an MCP caller could neither name a branch nor
    reconcile a dispatch whose supervisor died."""
    if action == "new":
        feat = feature or "current"
        return ops.dispatch_new(
            ctx.root,
            repo=repo,
            prompt=dispatch_require(prompt, "prompt", action),
            feature=feat,
            tasks=tasks,
            budget_usd=budget_usd,
            budget_reason=budget_reason,
            timeout_minutes=timeout_minutes,
            model=model,
            effort=effort,
            choice_reason=choice_reason,
            provider=provider,
            row=row,
            just_go=just_go,
            dry_run=dry_run,
            preview_id=preview_id,
            allow_unmatched=allow_unmatched,
            mcp=mcp,
            branch=branch,
            base=base,
            target=target,
            no_checks=no_checks,
            escalate_from=escalate_from,
            session_id=ctx.session_id(session_id, required=True),
            writes=writes,
            compare=compare,
            pr=pr,
        )
    if action == "list":
        return ops.dispatch_list(ctx.root, feature=feature, include_all=include_all)
    if action == "show":
        # The one-release alias: consumes exactly as before, and for the launching
        # or adopting manager also writes the verdict-read `dispatch consume` writes, since a
        # manager on the old schema has no `consume` to call.
        did = dispatch_require(dispatch_id, "dispatch_id", action)
        op = ops.dispatch_show(ctx.root, dispatch_id=did, caller=ctx.session_id(session_id))
        shown = as_dict_or_none(op.data)
        if shown is not None and "attempt" in shown:  # a synced id with no local record reads nothing
            _legacy_read(ctx, did, session_id, shown)
        return op
    if action == "consume":
        return ops.dispatch_consume(
            ctx.root,
            dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action),
            session_id=ctx.session_id(session_id, required=True),
        )
    if action == "spans":
        return ops.dispatch_spans(ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action))
    if action == "resume":
        if dry_run:
            raise AgentError("resume does not support dry_run=True; no dispatch was read or resumed")
        return ops.dispatch_resume(
            ctx.root,
            dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action),
            answer=dispatch_require(answer, "answer", action),
            answer_by="manager",
            budget_usd=budget_usd,
            budget_reason=budget_reason,
            timeout_minutes=timeout_minutes,
            allow_unmatched=allow_unmatched,
            again=again,
            writes=writes,
        )
    if action == "cancel":
        return ops.dispatch_cancel(ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action))
    if action == "abandon":
        return ops.dispatch_abandon(
            ctx.root,
            dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action),
            note=dispatch_require(message, "message", action),
            by="manager",
        )
    if action == "ack":
        return ops.dispatch_ack(
            ctx.root,
            dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action),
            note=message,
            by="manager",
        )
    if action == "say":
        return ops.dispatch_say(
            ctx.root,
            dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action),
            message=dispatch_require(message, "message", action),
        )
    if action == "collect":
        return ops.dispatch_collect(ctx.root, dispatch_id=dispatch_id, wait=wait)
    if action == "collect-files":
        return ops.dispatch_collect_files(
            ctx.root,
            dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action),
            paths=paths or [],
        )
    if action == "reverify":
        return ops.dispatch_reverify(
            ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action)
        )
    if action == "cleanup":
        return ops.dispatch_cleanup(
            ctx.root,
            feature=feature,
            dispatch_id=dispatch_id,
            force=force,
            dry_run=dry_run,
            delete_branches=delete_branches,
        )
    if action == "reconcile":
        return ops.dispatch_reconcile(
            ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action), apply=apply
        )
    if action == "restore":
        return ops.dispatch_restore(
            ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action), head=head, apply=apply
        )
    if action == "discard-files":
        return ops.dispatch_discard_files(
            ctx.root,
            dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action),
            paths=dispatch_require(paths, "paths", action),
            plan_hashes=plan_hashes,
            apply=apply,
        )
    if action == "watch":
        return ops.dispatch_watch(
            ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action), lines=lines
        )
    if action == "ledger":
        return ops.dispatch_ledger(ctx.root, by=by, refresh=refresh, repo=repo)
    if action == "plan-review":
        return ops.dispatch_plan_review(
            ctx.root,
            paths=dispatch_require(paths, "paths", action),
            session_id=ctx.session_id(session_id, required=True),
        )
    if action == "plan-ledger":
        return ops.dispatch_plan_ledger(
            ctx.root,
            paths=dispatch_require(paths, "paths", action),
            finding_id=finding_id,
            disposition=disposition,
            note=message,
            reraise_of=reraise_of,
        )
    raise AgentError(
        f"unknown dispatch action {action!r}"
    )  # unreachable: the schema enum already refuses this


def dispatch_read(
    ctx: Ctx,
    action: DispatchReadAction,
    dispatch_id: str | None = None,
    feature: str | None = None,
    include_all: bool = False,
    lines: int = ops.DISPATCH_WATCH_LINES,
    detail: bool = False,
) -> Op:
    """The read-only actions, which write no state: no notice consumption, no
    `chat_pending`, no verdict-read. Reading is not consuming; `dispatch consume` is."""
    if action == "show":
        return ops.dispatch_read_show(
            ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action), detail=detail
        )
    if action == "watch":
        return ops.dispatch_watch(
            ctx.root, dispatch_id=dispatch_require(dispatch_id, "dispatch_id", action), lines=lines
        )
    if action == "list":
        return ops.dispatch_list(ctx.root, feature=feature, include_all=include_all)
    raise AgentError(f"unknown dispatch_read action {action!r}")  # unreachable: the schema enum refuses it


DISPATCH_TOOLS: dict[str, ToolSpec] = {
    "dispatch": ToolSpec(
        dispatch,
        "Dispatch a worker",
        "Manage headless Claude or Codex workers (ADR-0006/0008), routed by `action`. To read a "
        "dispatch, use the `dispatch_read` tool, which changes nothing. Pick the action: "
        "launch `new`; continue a stopped worker `resume`; steer a running one `say`; stop it `cancel`; "
        "after reading a returned worker `consume`; close one nobody will resume `abandon`; mark one dealt "
        "with `ack`; where an attempt's time went `spans`; rerun its checks `reverify`; reconcile a dead "
        "supervisor `collect`; keep a worker's untracked files `collect-files`; remove worktrees `cleanup`; "
        "drop a lost checkout's registration `reconcile`; put an unfinished one back `restore`; delete an "
        "abandoned worker's scratch `discard-files`; outcomes and spend `ledger`; review a plan `plan-review`; "
        "read or dispose of a plan's review findings `plan-ledger`. "
        "Ids read `NNN-<tag>-SS` (e.g. 001-mac-01), or legacy `NNN-SS`. "
        "`new` (repo, prompt; feature defaults to current; a manager session id, so pass `session_id` from "
        "the session-start block when the server cannot see it) sends a self-contained work order into its "
        "own git worktree. A request needs no task numbers. A research request may omit `repo`: it runs "
        "read-only in the one registered harness repo, and refuses when none or several are registered. "
        "A captured (proposed) request refuses preview, launch and resume until it is set active. The tag comes from `machine:` in agent.local.yaml, else the enrolled device "
        "name, and `new` refuses without one (ADR-0036). Preview with dry_run=True; show the order and "
        "its work_order_url in inline code before asking to launch. Every launch question includes "
        "that link. Launch with the same fields and the returned preview_id after their yes; each "
        "prepared order keeps its own identity and bytes across a wave. Use just_go=True for a "
        "standing authorization. Pass the order text inline as prompt, never a .agent/scratch path. "
        "`row` names a rubric row "
        "(harness/dispatch-rubric.md) that supplies model and budget; a repo's pinned dispatch.model "
        "beats it; `choice_reason` says why; effort stays at the model's default unless `effort` is given "
        "with a reason. `provider` is claude or codex (a Codex budget is a soft cap). Pass `budget_reason` "
        "whenever `budget_usd` is above the default. `timeout_minutes` overrides the repo's clock; the "
        "preview names its source (passed, repo or default). Every command the work order names is checked "
        "against the worker's permission rules, and one with no matching rule refuses the launch unless "
        "allow_unmatched=True: put commands in `sh` fences and the rest in `text` fences (untagged fences "
        "are scored as commands; inline backticks are prose). `mcp` (agent.yaml `worker_mcp:` profiles) "
        "adds those MCP servers and their allow rules, and refuses while a profile's required file (a "
        "saved login) is missing; the owner's `bin/agent worker-mcp list` shows which are ready. `writes` "
        "declares what the dispatch may durably change: omitted means unknown and blocks every other writer "
        "in the repo; `[]` is a read-only review that blocks nothing but fails if it leaves changes "
        "(restore probe mutations); a list holds those paths, and each shared file goes in only one "
        "concurrent dispatch's `writes`. Non-exempt new and resumed launches need runnable dispatch.verify "
        "commands and explicit command/total timeouts; the dry-run verification field distinguishes "
        "configured-but-unverified, unconfigured and explicit exemptions, and planned checks are never "
        "verified results. no_checks=True exempts this dispatch from supervisor verification without making "
        "permissions read-only. `branch` names the worker's branch instead of the derived one (the repo's "
        "branch_template, ending `-<tag>-SS`) and `base` the ref it starts from (default "
        "`origin/<default_branch>`; a dispatch into this harness starts from locally landed main, pinned to "
        "its SHA). For row=review, `target` names the artifact branch or commit and supplies a pinned "
        "checkout base when `base` is omitted; an explicit `base` wins. A review also takes `compare` (the "
        "commit it is judged against, pinned to its SHA, kept apart from the checkout) and `pr` (a PR "
        "identity you were given; never infer one); both go into the synced handoff. A new commit to "
        "review is a new dispatch; resume only to discuss the same artifact. "
        "escalate_from=<id> reuses that dispatch's repo/feature/branch/worktree (so no `branch` "
        "or `base`), steps up one model rung, keeps the parent's recorded effort, including omission, unless "
        "`effort` is passed, and prefixes the work order with the old dispatch's last instruction; an "
        "omitted `repo` is the parent's. "
        "`resume` (dispatch_id, answer; budget_usd/budget_reason to raise the budget; timeout_minutes to "
        "lengthen the clock, else kept from launch; `writes` to narrow, never widen) rechecks the current "
        "verification configuration first. Recorded assessment is not a fresh rerun; legacy done records "
        "without an assessment remain unverified. A 4th attempt refuses unless again=True; escalate_from "
        "is the suggested way out. `new` and `resume` log their own dispatch/handoff entry; do not log it "
        "again. `new`, `resume`, `cancel`, `abandon`, `ack` and `say` return the record's key fields (id, "
        "state, attempt, cost, result summary, error, verification); verbose=True gives the full record. "
        "`consume` (dispatch_id, session_id) marks the current verdict read; only the launcher, or the "
        "manager adopting its feature once the launcher has ended, may. A new attempt starts unread. "
        "`say` (dispatch_id, message) puts the steer on the dispatch's thread and refuses when the "
        "supervisor is gone. `cancel` (dispatch_id) keeps worktree and branch. `abandon` (dispatch_id, "
        "message = the one-line reason) sets state `abandoned`, keeps the worktree and logs the reason. "
        "`ack` (dispatch_id, message optional) marks a finished dispatch dealt with (pushed, read): it "
        "leaves the Workers view and the session-start block, state unchanged, until a later resume. "
        "`abandon` and `ack` only when the owner says so. `spans` (dispatch_id): per attempt, time "
        "model-active, awaiting an answer, awaiting and running verification and awaiting landing, with "
        "base, harness revision and cost by source. `reverify` (dispatch_id) reruns only the supervisor "
        "checks for a terminal dispatch on its recorded worker head, in a fresh clean checkout it creates "
        "and removes: no model work, the worker worktree is only read, its uncommitted changes are reported "
        "as unverified, old evidence is kept, state is unchanged. It refuses a non-terminal dispatch, an "
        "active verification, a worktree whose HEAD moved, or a missing head. `collect` (dispatch_id "
        "optional; wait seconds) reconciles a dispatch whose supervisor died into its handoff and the "
        "feature log; with no dispatch_id, every one still waiting. `collect-files` (dispatch_id, paths) "
        "copies named untracked files from a stopped worker's worktree into that dispatch's private handoff "
        "artifacts. `cleanup` (feature or dispatch_id, force, dry_run, delete_branches) removes a feature's "
        "worktrees or one dispatch's tree and keeps branches unless delete_branches=True, which deletes "
        "removed trees' resolved branches no active or waiting dispatch needs. A running sibling on another "
        "tree does not block dispatch scope; all users of the selected tree must be stopped. A tree with "
        "unconfirmed verification or worker cleanup is `blocked` while the others go on; force never "
        "waives it. dry_run=True lists `would_remove`, the skipped, blocked and `missing` ones and removes "
        "nothing. Lists hold up to 10 examples beside `counts`, the rest in `evidence`. `reconcile` "
        "(dispatch_id, apply) drops a proven-missing worktree's Git registration once its HEAD and index "
        "are preserved, never a global prune; `restore` (dispatch_id, head, apply) puts an unfinished "
        "dispatch's lost checkout back on its branch at its recorded head without running a model or "
        "changing state; both preview unless apply=True. `discard-files` (dispatch_id, paths, plan_hashes, "
        "apply) previews an abandoned worker's named scratch with sha256s; only after the owner agrees, "
        "apply=True with plan_hashes = that preview's mapping deletes them. `ledger` (by: "
        "pr|feature|week, default pr; refresh=True refreshes PR data from gh first, optionally for one "
        "`repo`): finished dispatches grouped with spend and attempts. `plan-review` (paths = [the plan "
        "file], session_id) runs one blind review round of that plan, row review, no preview needed: two "
        "fresh read-only reviewers (Codex and Claude), then one verifier on their findings. The harness "
        "freezes and embeds the plan's bytes, so any later edit needs a new round. Each call advances the "
        "latest round on the current bytes one step, so call it again on the same plan after the "
        "supervisor reports a return: it puts both reviewers' findings in the ledger and launches the "
        "verifier, then records the verifier's table and completes the round. With no round, or a complete "
        "one, it launches a new round, even on unchanged bytes. A reviewer or verifier without a usable "
        "return is relaunched once; a second failure leaves the round for the owner. ExitPlanMode waits "
        "for a complete round on the exact version with no verified blocking finding unless the plan's "
        "first lines say `review: small — <reason>` or `review: none — <reason>`. It works in plan mode. "
        "`plan-ledger` (paths = [the plan file]) returns that plan's findings ledger across "
        "review rounds (finding, reviewer, round, verifier result, disposition) and its disputes: a "
        "Claude reviewer's finding the verifier refuted or called optional, and a rejected finding a later "
        "round raised again. Never show the ledger to a reviewer. With finding_id and disposition=fixed "
        "(message = the changed section) or disposition=rejected (message = the reason) it records your "
        "disposition; with finding_id and reraise_of=<earlier id> it links a re-raise. It does not run in "
        "plan mode. `list`, `show` and `watch` stay one release as `dispatch_read` aliases; this `show` returns "
        "the full record, clears chat-pending and, for the launcher or adopting manager, marks the verdict read.",
        destructive=True,
    ),
    "dispatch_read": ToolSpec(
        dispatch_read,
        "Read a dispatch",
        "Read workers without changing anything: no consumption, no flags cleared, no "
        "permission prompt. `show` (dispatch_id) gives a concise summary: state, the worker's verdict "
        "line and open questions, recorded verification, whether the verdict is read, and evidence links "
        "(handoff, branch, worktree); detail=True gives the full record (for a tagged id synced from "
        "another machine, its handoff and owner). After reading a returned worker, mark it read with the "
        "`dispatch` tool, action `consume`. `watch` (dispatch_id, lines, default 40) is the bounded "
        "liveness view to poll: compact status, last activity, what it waits on, and the last `lines` "
        "digest lines of the transcript. `list` (feature, include_all) is non-terminal and recent "
        "dispatches by default.",
        read_only=True,
    ),
}


# ---------------------------------------------------------------- worker tools


def worker_log(ctx: Ctx, kind: WorkerLogKind, text: str) -> Op:
    if kind not in WORKER_KINDS:
        raise AgentError(f"kind must be one of {', '.join(WORKER_KINDS)}; got {kind!r}")
    return ops.log(
        ctx.root,
        kind=kind,
        text=text.rstrip("\n") + f" (dispatch {ctx.dispatch_id})",
        feature=str(ctx.feature),
    )


WORKER_TOOLS: dict[str, ToolSpec] = {
    "log": ToolSpec(
        worker_log,
        "Log entry",
        "Record something worth remembering in the feature log: dead-end (what did not work), surprise (unexpected behavior), "
        "measurement (a number), blocker (what you are waiting on). Quotes and newlines are safe here. The feature and dispatch id are attached for you.",
    ),
}
