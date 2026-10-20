"""Expose manager tools over feature, task, log, ADR, status and capture operations."""

from __future__ import annotations

from typing import Literal

from .. import ops
from ..ops import lessons as lesson_ops
from .tools import (
    FEATURE_REF,
    SESSION_ARG,
    AdrStatus,
    Ctx,
    FeatureStatus,
    LogKind,
    Op,
    OpenStatus,
    TerminalStatus,
    ToolSpec,
)

# ---------------------------------------------------------------- manager tools


def lesson(
    ctx: Ctx,
    action: Literal["add", "list", "resolve", "draft_check"],
    title: str | None = None,
    scope: Literal["harness", "private", "all"] = "all",
    kind: Literal["lesson", "practice"] = "lesson",
    source: str = "",
    evidence: str | None = None,
    home: str = "",
    entry_id: str | None = None,
    disposition: Literal["task", "mechanism", "prose", "practice", "dropped", "upstream", "config", "repo"]
    | None = None,
    note: str | None = None,
    body_file: str | None = None,
) -> Op:
    """One tool over the lesson queue, routed by `action` the way `dispatch` is,
    so the queue costs one slot of the ~35 tool budget (see ADR-0007)."""
    from ..core import AgentError

    if action == "list":
        return lesson_ops.lesson_list(ctx.root, scope=scope)
    if action == "add":
        if not title or not evidence:
            raise AgentError("add needs title and evidence")
        return lesson_ops.lesson_add(
            ctx.root, title=title, scope=scope, kind=kind, source=source, evidence=evidence, home=home
        )
    if action == "draft_check":
        if not title or not body_file:
            raise AgentError("draft_check needs title and body_file")
        return lesson_ops.lesson_draft_check(ctx.root, title=title, body_file=body_file)
    if not entry_id or not disposition or not note:
        raise AgentError("resolve needs entry_id, disposition and note")
    return lesson_ops.lesson_resolve(ctx.root, entry_id=entry_id, disposition=disposition, note=note)


def feature_new(
    ctx: Ctx,
    slug: str,
    title: str,
    status: OpenStatus = "active",
    tickets: list[str] | None = None,
    repos: list[str] | None = None,
    tags: list[str] | None = None,
    use: bool = True,
    session_id: str | None = None,
) -> Op:
    return ops.feature_new(
        ctx.root,
        slug=slug,
        title=title,
        status=status,
        tickets=tickets,
        repos=repos,
        tags=tags,
        use=use,
        session_id=ctx.session_id(session_id, required=use),
    )


def feature_list(ctx: Ctx, status: list[FeatureStatus] | None = None, include_closed: bool = False) -> Op:
    return ops.feature_list(
        ctx.root, status=list(status) if status is not None else None, include_closed=include_closed
    )


def feature_show(ctx: Ctx, feature: str = "current", session_id: str | None = None) -> Op:
    return ops.feature_show(ctx.root, feature=feature, session_id=ctx.session_for(feature, session_id))


def feature_use(ctx: Ctx, feature: str, session_id: str | None = None, switch: bool = False) -> Op:
    return ops.feature_use(
        ctx.root, feature=feature, session_id=ctx.session_id(session_id, required=True), switch=switch
    )


def feature_set(
    ctx: Ctx,
    feature: str,
    status: FeatureStatus | None = None,
    title: str | None = None,
    add_ticket: list[str] | None = None,
    remove_ticket: list[str] | None = None,
    add_repo: list[str] | None = None,
    remove_repo: list[str] | None = None,
    add_adr: list[int] | None = None,
    remove_adr: list[int] | None = None,
    add_tag: list[str] | None = None,
    remove_tag: list[str] | None = None,
    session_id: str | None = None,
    reason: str | None = None,
) -> Op:
    return ops.feature_set(
        ctx.root,
        feature=feature,
        session_id=ctx.session_for(feature, session_id),
        status=status,
        title=title,
        add_ticket=add_ticket,
        remove_ticket=remove_ticket,
        add_repo=add_repo,
        remove_repo=remove_repo,
        add_adr=add_adr,
        remove_adr=remove_adr,
        add_tag=add_tag,
        remove_tag=remove_tag,
        reason=reason,
    )


def feature_close(
    ctx: Ctx,
    feature: str,
    status: TerminalStatus = "done",
    note: str | None = None,
    session_id: str | None = None,
) -> Op:
    return ops.feature_close(
        ctx.root,
        feature=feature,
        status=status,
        note=note,
        session_id=ctx.session_for(feature, session_id),
    )


def task_list(
    ctx: Ctx, feature: str = "current", open_only: bool = False, session_id: str | None = None
) -> Op:
    return ops.task_list(
        ctx.root, feature=feature, open_only=open_only, session_id=ctx.session_for(feature, session_id)
    )


def task_done(
    ctx: Ctx, task: str, feature: str = "current", note: str | None = None, session_id: str | None = None
) -> Op:
    return ops.task_done(
        ctx.root, task=task, feature=feature, note=note, session_id=ctx.session_for(feature, session_id)
    )


def task_undo(ctx: Ctx, task: str, feature: str = "current", session_id: str | None = None) -> Op:
    return ops.task_undo(
        ctx.root, task=task, feature=feature, session_id=ctx.session_for(feature, session_id)
    )


def task_add(ctx: Ctx, text: str, feature: str = "current", session_id: str | None = None) -> Op:
    return ops.task_add(ctx.root, text=text, feature=feature, session_id=ctx.session_for(feature, session_id))


def task_supersede(
    ctx: Ctx, task: str, reason: str, link: str, feature: str = "current", session_id: str | None = None
) -> Op:
    return ops.task_supersede(
        ctx.root,
        task=task,
        reason=reason,
        link=link,
        feature=feature,
        session_id=ctx.session_for(feature, session_id),
    )


def task_check(ctx: Ctx, feature: str = "current", session_id: str | None = None) -> Op:
    return ops.task_check(ctx.root, feature=feature, session_id=ctx.session_for(feature, session_id))


def log(ctx: Ctx, kind: LogKind, text: str, feature: str = "current", session_id: str | None = None) -> Op:
    return ops.log(
        ctx.root, kind=kind, text=text, feature=feature, session_id=ctx.session_for(feature, session_id)
    )


def adr_new(
    ctx: Ctx,
    slug: str,
    title: str,
    feature: str | None = None,
    status: AdrStatus = "proposed",
    supersedes: int | None = None,
    session_id: str | None = None,
    scope: Literal["notes", "harness"] = "notes",
) -> Op:
    sid = ctx.session_for(feature, session_id) if feature else None
    return ops.adr_new(
        ctx.root,
        slug=slug,
        title=title,
        feature=feature,
        status=status,
        supersedes=supersedes,
        session_id=sid,
        scope=scope,
    )


def status_render(ctx: Ctx) -> Op:
    return ops.status_render(ctx.root)


# Keep journal collection behind the harness action rather than spend a separate tool slot on a
# duplicate surface (see ADR-0007).


def raw_preserve(ctx: Ctx, feature: str = "current", session_id: str | None = None) -> Op:
    return ops.raw_preserve(ctx.root, feature=feature, session_id=ctx.session_for(feature, session_id))


FEATURE_TOOLS: dict[str, ToolSpec] = {
    "feature_new": ToolSpec(
        feature_new,
        "New feature",
        "Create features/NNN-slug (plan.md, log.md) and map this session to it (use=True). "
        "Slug is kebab-case, two to four words; a ticket id (TK#123, JIRA-45) goes in "
        "`tickets`, not the slug. `repos` are registry names or shorts. Returns the feature dict. "
        + SESSION_ARG,
    ),
    "feature_list": ToolSpec(
        feature_list,
        "List features",
        "List open features (all statuses with include_closed=True, or filter with `status`). Returns items and count.",
        read_only=True,
    ),
    "feature_show": ToolSpec(
        feature_show,
        "Show feature",
        "One feature with task counts and its last five log entries. " + FEATURE_REF + " " + SESSION_ARG,
        read_only=True,
    ),
    "feature_use": ToolSpec(
        feature_use,
        "Map session to feature",
        "Map this session only when the owner asks you to work on that feature; earlier unassigned "
        "captures move with it. Inspecting, answering or cleaning up another feature's worker "
        "keeps the mapping: use explicit `feature`/`dispatch_id` arguments. A different mapping "
        "with open work, or mapping after a peer message, requires `switch=true` and the owner's "
        "ask. Every changed mapping is logged. " + FEATURE_REF + " " + SESSION_ARG,
        idempotent=True,
    ),
    "feature_set": ToolSpec(
        feature_set,
        "Edit feature fields",
        "Edit frontmatter fields: status, title, add/remove ticket, repo, adr, tag. Use this instead of editing the frontmatter by hand. "
        "A status change is logged with `reason`; give one for `blocked` (what it waits on), and set `active` again once that is gone. "
        + FEATURE_REF,
        idempotent=True,
    ),
    "feature_close": ToolSpec(
        feature_close,
        "Close feature",
        "Close a feature as done or abandoned; sets the closed date and re-renders the vault views. `note` writes a closing decision entry first. Only when the owner says to close. "
        "Refused while a task is open: tick it or supersede it first. "
        "Run `/retro` first unless the owner says to skip it; the close does not check. "
        "After closing, write today's journal (`/journal today`) on the journal machine. " + FEATURE_REF,
    ),
    "task_list": ToolSpec(
        task_list,
        "List tasks",
        "Tasks under ## Tasks with their numbers; open_only=True hides ticked ones. " + FEATURE_REF,
        read_only=True,
    ),
    "task_done": ToolSpec(
        task_done,
        "Tick task",
        "Tick a task by number or unique substring. Call this before telling the owner something is finished. `note` is appended in parentheses (a commit sha, 'via /intake'). "
        "Use this instead of editing the checkbox. Idempotent: an already-done task returns changed=False. "
        + FEATURE_REF,
        idempotent=True,
    ),
    "task_undo": ToolSpec(
        task_undo,
        "Untick task",
        "Reopen a ticked task by number or unique substring. " + FEATURE_REF,
        idempotent=True,
    ),
    "task_add": ToolSpec(
        task_add,
        "Add task",
        "Append a one-line task with a verifiable outcome to ## Tasks. Add a task before starting new work. "
        + FEATURE_REF,
    ),
    "task_supersede": ToolSpec(
        task_supersede,
        "Supersede task",
        "Mark a task superseded (`- [-]`): no longer wanted as written, or moved to another feature. "
        "`reason` says why; `link` says where it went (a commit, `NNN #k`, a feature or a URL). It keeps its "
        "number and counts as neither open nor done. The only way to write `[-]`; never edit the checkbox. "
        + FEATURE_REF,
        idempotent=True,
    ),
    "task_check": ToolSpec(
        task_check,
        "Check open tasks",
        "Read-only evidence that open tasks may already be done or stale: commits since each task was added "
        "that touch the paths it names, landings and ticks elsewhere that mention it, and the age of the plan's "
        "Context. `feature_use` and a feature's first dispatch of a session run it already. " + FEATURE_REF,
        read_only=True,
    ),
    "log": ToolSpec(
        log,
        "Log entry",
        "Append a hindsight entry to the feature's log.md. Quotes, backticks, `$`, and newlines are safe here. "
        "Pick the narrowest kind: measurement for a number, dead-end for what did not work, deviation for a plan change, blocker when waiting, "
        "surprise for unexpected behavior, decision for a choice, note otherwise. Write for the owner in six months: what, why, what we would do differently. "
        + FEATURE_REF,
    ),
    "lesson": ToolSpec(
        lesson,
        "Lesson queue",
        "The queue for findings with no open feature to hold them: from a closed feature, a retro, or none. "
        "A finding from an open feature still goes to that feature. Every lesson goes to the one local queue in the notes "
        "repo; `scope` is accepted and ignored. `add` (title, evidence, kind, source, home): evidence must state the "
        "symptom or owner quote on its own. `list`. `resolve` (entry_id, disposition "
        "task|mechanism|prose|practice|dropped|upstream|config|repo, note naming the task, commit, file, issue URL or "
        "reason). `draft_check` (title, body_file): before filing an upstream issue, returns the upstream repo, a "
        "fallback URL and `truncated`. Review with /lesson review.",
    ),
    "adr_new": ToolSpec(
        adr_new,
        "New decision",
        "Create a private decision in the linked notes repo by default. Pass scope=harness only for a public harness design ADR; its public file never links private features. Product ADRs belong in the product repo through /dispatch. Fill Context, Decision, Consequences, Alternatives after creation.",
    ),
    "status_render": ToolSpec(
        status_render,
        "Render vault views",
        "Regenerate public adrs/README.md, private decisions/README.md, Dashboard.md, Index.md, bases/ and .agent/status.json. Never edit those by hand.",
        idempotent=True,
    ),
    "raw_preserve": ToolSpec(
        raw_preserve,
        "Preserve raw transcripts",
        "Copy every transcript this feature's raw captures point at into .agent/raw/NNN-slug/transcripts/ so a retro can "
        "run after Claude Code's 30-day cleanup; feature_close does this on its own. " + FEATURE_REF,
        idempotent=True,
    ),
}
