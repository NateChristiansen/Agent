"""Expose manager tools over repository, session, lease and peer operations."""

from __future__ import annotations

from typing import Literal

from .. import ops
from ..core import AgentError
from .tools import SESSION_ARG, Ctx, LeaseAction, Op, ToolSpec, dispatch_require

# ---------------------------------------------------------------- manager tools


def repos_list(ctx: Ctx) -> Op:
    return ops.repos_list(ctx.root)


def repos_validate(ctx: Ctx) -> Op:
    return ops.repos_validate(ctx.root)


def repos_status(ctx: Ctx, name: str | None = None, fetch: bool = False) -> Op:
    return ops.repos_status(ctx.root, name=name, fetch=fetch)


def repos_sync(
    ctx: Ctx,
    name: str | None = None,
    no_clone: bool = False,
    fetch: bool = False,
    pull: bool = False,
    harness: bool = False,
) -> Op:
    return ops.repos_sync(ctx.root, name=name, no_clone=no_clone, fetch=fetch, pull=pull, harness=harness)


def session_get(ctx: Ctx, session_id: str | None = None) -> Op:
    return ops.session_get(ctx.root, session_id=ctx.session_id(session_id, required=True))


def owner_question(
    ctx: Ctx,
    summary: str | None = None,
    session_id: str | None = None,
    resolution: Literal["answered", "withdrawn", "replaced"] | None = None,
) -> Op:
    return ops.owner_question(
        ctx.root, session_id=ctx.session_id(session_id, required=True), summary=summary, resolution=resolution
    )


def lease(
    ctx: Ctx,
    action: LeaseAction,
    area: str | None = None,
    note: str | None = None,
    timeout: float = 120,
    session_id: str | None = None,
    holder: str | None = None,
    confirm: bool = False,
    ask: str | None = None,
) -> Op:
    """One tool over `ops.lease_*` (item 3, shaped like the `dispatch` tool). `action` picks
    the operation. `area` is required for take/release/wait, `session_id` for every action but
    `list` and `clear`. `clear` frees every lease `holder` (a session id or name) holds, only
    when the owner asks; `confirm` is needed when the holder was active recently."""
    if action == "clear":
        return ops.lease_clear(ctx.root, who=dispatch_require(holder, "holder", action), confirm=confirm)
    if action == "take":
        return ops.lease_take(
            ctx.root,
            area=dispatch_require(area, "area", action),
            note=note,
            session_id=ctx.session_id(session_id, required=True),
        )
    if action == "release":
        return ops.lease_release(
            ctx.root,
            area=dispatch_require(area, "area", action),
            session_id=ctx.session_id(session_id, required=True),
        )
    if action == "list":
        return ops.lease_list(ctx.root)
    if action == "wait":
        return ops.lease_wait(
            ctx.root,
            area=dispatch_require(area, "area", action),
            note=note,
            timeout=timeout,
            session_id=ctx.session_id(session_id, required=True),
            ask=ask,
        )
    raise AgentError(f"unknown lease action {action!r}")  # unreachable: the schema enum already refuses this


PeerAction = Literal["send", "inbox", "list"]


def peer(
    ctx: Ctx,
    action: PeerAction,
    to: str | None = None,
    message: str | None = None,
    peek: bool = False,
    session_id: str | None = None,
    expect_reply: bool = False,
    reply_timeout: float = 600,
) -> Op:
    """One tool over `ops.peer_send|inbox|list`, shaped like `lease` (a manager with no peer tool reached for SendMessage, #67). Plan pairing is gone: a plan's review is
    one fresh worker on the other client (`dispatch` action `plan-review`)."""
    if action == "list":
        return ops.peer_list(ctx.root)
    sid = ctx.session_id(session_id, required=True)
    if action == "send":
        return ops.peer_send(
            ctx.root,
            to=dispatch_require(to, "to", action),
            message=dispatch_require(message, "message", action),
            session_id=sid,
            expect_reply=expect_reply,
            reply_timeout=reply_timeout,
        )
    if action == "inbox":
        return ops.peer_inbox(ctx.root, session_id=sid, peek=peek)
    raise AgentError(f"unknown peer action {action!r}")  # unreachable: the schema enum already refuses this


REPO_TOOLS: dict[str, ToolSpec] = {
    "repos_validate": ToolSpec(
        repos_validate,
        "Validate config",
        "Validate agent.yaml and this machine's overrides. Unknown shared, repo and dispatch keys "
        "are errors with spelling hints. Returns ok and problems.",
        read_only=True,
    ),
    "repos_list": ToolSpec(
        repos_list,
        "List repos",
        "The repo registry (agent.yaml) with clone state. Returns items and count.",
        read_only=True,
    ),
    "repos_status": ToolSpec(
        repos_status,
        "Repo status",
        "git branch, ahead/behind, dirty count and worktrees per registered repo (or one by `name`). fetch=True fetches first.",
        read_only=True,
    ),
    "repos_sync": ToolSpec(
        repos_sync,
        "Sync repos",
        "Clone missing repos and repair worktrees. pull=True also fetches and fast-forwards the "
        "default branch, skipping any clone that is dirty or off its default branch. Run this with "
        "pull=True before reading a product repo: research against a stale clone reports last "
        "week's code as current truth. harness=True instead fetches main from the URL pinned as "
        "harness.sync_url in agent.local.yaml and fast-forwards the harness's own main to it, "
        "refusing if main has commits it lacks: the way a machine that lands through another "
        "reaches the pushed tip (ADR-0041). git pull on main is always refused.",
    ),
    "session_get": ToolSpec(
        session_get,
        "Session record",
        "This session's record: mapped feature, cwd, timestamps. " + SESSION_ARG,
        read_only=True,
    ),
    "owner_question": ToolSpec(
        owner_question,
        "Wait on the owner",
        "How a Codex manager asks the owner outside Plan mode: put the question in your visible "
        "reply, then call this with the question as `summary` before ending the turn. The tab shows "
        "the hand until you explicitly resolve it with resolution='answered', 'withdrawn' or "
        "'replaced'; an unrelated prompt leaves it pending. A different summary replaces the old "
        "question. The ask guard counts the question as asked only "
        "when `summary` matches it. Claude Code asks through AskUserQuestion and Codex Plan mode "
        "through request_user_input; each raises the hand itself, so do not call this after one. "
        "Never use request_user_input_async: its questions never reach the owner. " + SESSION_ARG,
        idempotent=True,
    ),
    "lease": ToolSpec(
        lease,
        "Take, release, wait on, list or clear area leases",
        "Manage an advisory lock under .agent/leases, routed by `action`: `take`/`release`/`wait` need "
        "`area`, `wait` also takes `timeout` (seconds, default 120) and `ask` (why you need it and which "
        "files; it messages the holder first), and blocks until the area frees or this "
        "session already holds it (raising the same one-line wait-command message on timeout that `take` "
        "raises immediately when another live session holds it), and `list` returns every lease as live, "
        "resting, idle, dead or unverified (an unknown-pid holder quiet 30 minutes; it still blocks). "
        "`clear` (holder: a session id or name) frees every lease that holder has, only when the owner "
        "asks; it returns needs_confirm, and clears nothing, if the holder touched one in the last 30 "
        "minutes, until called again with confirm=True after the owner agrees. An area is a `vault:` path or `repo:<short>`. Prefer the narrowest path you are "
        "actually working in — `vault:src/agent_cli/dispatch`, `vault:harness/sessions.md` — because two areas "
        "block each other only when their paths overlap, so a sibling package does not have to wait. "
        "`vault:src` and `vault:harness` still lock a whole subtree, which is what an edit hook takes when "
        "it cannot tell. " + SESSION_ARG,
    ),
    "peer": ToolSpec(
        peer,
        "Message another manager",
        "Reach another manager session, Claude or Codex, routed by `action`. `send` (to, message) "
        "queues to its inbox and wakes it; set expect_reply=True for a request you wait on, with "
        "reply_timeout in seconds (default 600). The wait clears on a message from that peer or "
        "its deadline. Use this, not SendMessage, for any manager. `list` names "
        "who is reachable; `inbox` reads yours (peek=True keeps them). A plan's cross-client review "
        "is the `dispatch` tool's `plan-review` action, not a peer. " + SESSION_ARG,
    ),
}
