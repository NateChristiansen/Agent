"""Session lifecycle hooks: `session-start` (the reorientation block, feature mapping, and the
deterministic nags — lessons, journal, unassigned raw, dirty src, other live sessions),
`capture` (routes each hook event into the session's raw log), `dispatch-inbox`
(UserPromptSubmit: finished/blocked/questioning workers), and `render-after-plan-edit` (keep
generated views in sync with a plan.md edit).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import capture as capture_mod
from .. import sessions, status
from ..core import AgentError, agent_dir_name, agent_display, today
from ..records.narrow import dig
from . import session_budget as budget
from .paths import relative
from .session_blocks import (
    dirty_src_warning,
    journal_nag,
    lesson_nag,
    open_tasks_block,
    other_sessions_block,
    unassigned_nag,
    unassigned_summary,
)

# ------------------------------------------------------------------ session-start


def session_start(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("agent_id") or payload.get("agent_type"):
        return ""
    session_id = str(payload.get("session_id") or "")
    source = str(payload.get("source") or payload.get("session_reason") or "startup")
    cwd = payload.get("cwd")
    # Both clients honour `hookSpecificOutput.additionalContext` on SessionStart, so the whole
    # block below is shared (verified against codex-cli 0.155.1's embedded
    # `session-start.command.output` schema). Only the extras differ, and Codex's schema is
    # `additionalProperties: false`, so an extra key risks the whole output being rejected.
    provider = sessions.resolve_provider()

    # Pull the notes repo first, so the block below shows what arrived (see ADR-0034). It never
    # raises, and with the switch off it runs no git command.
    from . import notes_sync as notes_hook

    notes_result = notes_hook.startup_pull(root)
    from ..usage import job as usage_job

    usage_job.trigger_quietly(root, "session-start")

    digest = digest_full = ""
    try:
        data = status.collect(root)
        digest = budget.status_digest(data)
        digest_full = budget.status_digest(data, limit=None)
    except Exception:  # noqa: BLE001 — a reporting failure must not stop session start
        digest = "Open features could not be collected; use feature_list or Dashboard.md."

    if not session_id:
        feature_line = (
            "no session_id in hook input; cannot map a feature. Captures go to "
            f"{agent_display('raw', 'unassigned')}/."
        )
    else:
        # One resolver for the by-pid map and the record's `pid`. Ops and the MCP server share
        # it, so all three agree on whose pid owns this session (sessions.resolve_pid).
        pid, known = sessions.resolve_identity()
        feature_line = _map_session(root, session_id, cwd, pid)
        _map_identity(root, session_id, pid, known)
        _map_pid(root, session_id, pid)
        _map_provider(root, session_id, provider)
        notes_hook.mark_pull(root, session_id, pid, notes_result)
        # the `activity` hook runs alongside this one and may write the snapshot before the pid lands
        from ..vault import live

        live.write_quietly(root)

    if source in ("compact", "resume"):
        head = f"[manager reorientation — source: {source}. Re-read this block before acting on the summary.]"
    else:
        head = f"[manager session — source: {source}]"

    from .. import setup as setup_mod

    problems = setup_mod.fresh_clone_warnings(root)
    try:
        from .. import land_recover

        problems += land_recover.report(root, include_certificate=False)
    except Exception:  # noqa: BLE001, S110 — session start never fails on a report
        pass
    made, link_problems = _ensure_skill_links(root)
    problems += link_problems
    warn = "\n" + "".join(f"WARNING: {p}\n" for p in problems) if problems else ""
    try:
        from .session_blocks import full_pass_block

        if full_pass := full_pass_block(root):
            warn += "\n" + full_pass + "\n"
    except Exception:  # noqa: BLE001, S110 — a reporting failure never stops session start
        pass
    if made:
        warn += "\nmade skill link(s): " + ", ".join(made) + "\n"

    pending = ""
    pending_full = ""
    try:
        from .. import dispatch as dispatch_mod

        pending = dispatch_mod.pending_block(root, session_id=session_id, compact=True)
        pending_full = dispatch_mod.pending_block(root, session_id=session_id)
    except Exception:  # noqa: BLE001
        pending = pending_full = ""
    if session_id:
        stuck = _stuck_tasks_line(root, session_id)
        if stuck:
            pending = (pending + "\n\n" + stuck).strip("\n")
            pending_full = (pending_full + "\n\n" + stuck).strip("\n")

    # Messages another session queued with `peer send`. Taken rather than peeked:
    # a message is delivered once, and a session that never starts again never reads it. Without
    # this the mailbox existed and nobody looked in it — the owner found that immediately, by
    # asking why a message had not been read.
    inbox = ""
    if session_id:
        try:
            from .. import peers as peers_mod

            rows = peers_mod.take(root, session_id, via="session_start")
            block = peers_mod.block(rows)
            if block:
                inbox = "\n" + block + "\n"
        except Exception:  # noqa: BLE001
            inbox = ""

    nag = ""
    try:
        nag = lesson_nag(root)
    except Exception:  # noqa: BLE001
        nag = ""

    jnag = ""
    try:
        jnag = journal_nag(root)
        if jnag:
            from ..journal import pending as pending_mod

            if session_id and (current := pending_mod.fingerprint(root, today())):
                from .stop import JOURNAL_SEEN_KEY

                sessions.set_field(root, session_id, f"{JOURNAL_SEEN_KEY}:{today().isoformat()}", current)
    except Exception:  # noqa: BLE001
        jnag = ""

    unassigned = unassigned_full = ""
    try:
        unassigned = unassigned_summary(root)
        unassigned_full = unassigned_nag(root)
    except Exception:  # noqa: BLE001
        unassigned = unassigned_full = ""

    others = others_full = ""
    try:
        others_full = other_sessions_block(root, session_id)
        others = budget.clip(others_full, 500)
    except Exception:  # noqa: BLE001
        others = others_full = ""

    dirty = ""
    try:
        dirty = dirty_src_warning(root)
    except Exception:  # noqa: BLE001
        dirty = ""

    notes = ""
    try:
        notes = notes_hook.startup_line(root, session_id, notes_result) or ""
    except Exception:  # noqa: BLE001
        notes = ""

    tasks_full = ""
    if session_id:
        try:
            mapped = (sessions.get(root, session_id) or {}).get("feature")
            if mapped:
                tasks_full = open_tasks_block(root, mapped, limit=None).strip("\n")
        except Exception:  # noqa: BLE001
            tasks_full = ""

    from .. import repos as repos_mod

    name = repos_mod.owner_name(root)
    owner_line = (
        f'owner: {name}. Every harness file, skill and tool says "the owner"; that is {name}.'
        if name
        else "owner: not set. Add `owner: {name: <your name>}` to agent.yaml so the harness knows who it works for."
    )
    warn_text = warn.strip("\n")
    parts = {
        "warn": budget.clip(warn_text, 600),
        "dirty": dirty,
        "notes": budget.clip(notes, 900),
        "others": others,
        "pending": budget.clip(pending, 1500),
        "lessons": budget.clip(nag, 350),
        "journal": budget.clip(jnag, 350),
        "unassigned": budget.clip(unassigned, 300),
        "digest": digest,
    }
    full_path = budget.full_list_display(session_id)
    wrote = budget.write_full_lists(
        root,
        session_id,
        [
            ("Warnings", warn_text),
            ("Uncommitted src", dirty),
            ("Notes", notes),
            ("Other live sessions", others_full),
            ("Dispatches", pending_full),
            ("Lessons", nag),
            ("Journal", jnag),
            ("Unassigned raw captures", unassigned_full),
            ("Open tasks", tasks_full),
            ("Open features", digest_full),
        ],
    )
    pointer = (
        f'full lists: {full_path} — read it before acting on any "N more"'
        if wrote
        else "full lists: could not be written; use the tools named in each line"
    )

    def wrap(text: str) -> str:
        return f"\n{text}\n" if text else ""

    def assemble(p: dict[str, str], moved_note: str) -> str:
        digest_block = p["digest"] or "Open features: digest moved to the full-list file."
        return (
            f"{head}\n"
            f"{owner_line}\n"
            f"session_id: {session_id}\n"
            f"{feature_line}\n{wrap(p['others'])}{wrap(p['dirty'])}{wrap(p['notes'])}{wrap(p['warn'])}"
            f"{inbox}{wrap(p['pending'])}{wrap(p['lessons'])}{wrap(p['journal'])}{wrap(p['unassigned'])}\n"
            f"{wrap(moved_note)}{pointer}\n"
            f"{digest_block}\nFull feature list: feature_list or Dashboard.md; older material: Index.md.\n\n"
            "Never assume; ask the owner. Record with the agent tools: log, task_done, task_add, feature_use "
            "(mcp__agent__*). Never run bin/agent in a shell; if the agent MCP server is down, ask the owner to reconnect it. "
            "Do not hand-edit Dashboard.md, Index.md, bases/, features/*/log.md, "
            f"adrs/README.md, or {agent_dir_name()}/. One shell command per Bash call."
        )

    ctx, _ = budget.fit(parts, assemble, full_path)
    out: dict[str, Any] = {"hookEventName": "SessionStart", "additionalContext": ctx}
    # Codex honours no `sessionTitle` in any of its hook output schemas, and rejects unknown
    # keys, so the rename is Claude-only and a Codex session keeps the name the harness gives
    # it (`hooks/activity.py`). Tracked in harness/provider-gaps.md.
    if provider == "claude":
        try:
            from .naming import title_for

            title = title_for(root, payload)
            if title:
                out["sessionTitle"] = title  # same effect as /rename
        except Exception:  # noqa: BLE001, S110 — naming is a courtesy
            pass
    return json.dumps({"hookSpecificOutput": out})


def _ensure_skill_links(root: Path) -> tuple[list[str], list[str]]:
    """Make any `.claude/skills/` link a pull left missing.

    The links are gitignored and setup makes them, so a skill a `git pull` added under
    `.agents/skills/` stayed invisible to Claude until setup ran again. `skill_links.ensure`
    is one directory listing plus a stat per skill, makes only what is missing or stale, and
    never touches a real file or directory, so it is safe here. Returns the links made and a
    warning per link it refused or failed to make; a `skipped` link (submodule not checked
    out) is the fresh-clone warning's to report. If `ensure` itself raises (an unreadable
    skills directory, a link loop), session start still succeeds and says the repair did not
    finish, rather than hiding the gap. This proves the link on disk; whether Claude lists a
    skill linked during this SessionStart in the same session is not verified."""
    try:
        from .. import skill_links

        rows = skill_links.ensure(root)
    except Exception as exc:  # noqa: BLE001 — a hook never raises; failing open is the contract
        return [], [
            f"skill link repair did not finish ({type(exc).__name__}); the `harness` tool's `doctor` "
            "action, or setup, shows which skills have no link"
        ]
    made = [Path(r["link"]).as_posix() for r in rows if r["state"] in ("created", "replaced")]
    problems = [
        f"skill link {Path(r['link']).as_posix()} {r['state']}: {r.get('detail', '')}; the link to "
        f"{Path(r['target']).as_posix()} was not repaired"
        for r in rows
        if r["state"] in ("refused", "failed")
    ]
    return made, problems


def _map_provider(root: Path, session_id: str, provider: str) -> None:
    """Record an explicit provider for both clients so views, ledger and cost readers share the same
    label.
    """
    try:
        record = sessions.get(root, session_id) or {}
        if record.get("provider") != provider:
            sessions.set_field(root, session_id, "provider", provider)
    except Exception:  # noqa: BLE001, S110 — a label is not worth failing a session start
        pass


def _map_pid(root: Path, session_id: str, pid: int | None) -> None:
    """Record which Claude process owns this session (`.agent/sessions/by-pid/<pid>`).

    The MCP server shares that parent process, so it can find its session id without being
    told. Fail open."""
    try:
        if pid is not None:
            sessions.write_by_pid(root, pid, session_id)
    except Exception:  # noqa: BLE001, S110 — a hook never raises; failing open is the contract
        pass


def _map_identity(root: Path, session_id: str, pid: int | None, known: bool) -> None:
    """Record an unknown pid as unknown, replacing a daemon pid an older build wrote. Fail open."""
    try:
        from ..session_liveness import set_identity

        set_identity(root, session_id, pid, known)
    except Exception:  # noqa: BLE001, S110 — a hook never raises; failing open is the contract
        pass


def _map_session(root: Path, session_id: str, cwd: Any, pid: int | None = None) -> str:
    try:
        record = sessions.set_record(root, session_id, cwd=str(cwd) if cwd else None, pid=pid)
    except Exception:  # noqa: BLE001
        record = None
    feature = (record or {}).get("feature")
    if feature:
        line = f"active feature: {int(feature['id']):03d} {feature['slug']}  ({feature['dir']})"
        return line + _blocked_block(root, feature) + open_tasks_block(root, feature)
    return (
        "no feature mapped to this session. When the owner assigns feature work, call the "
        f"`feature_use` tool (session_id {session_id}), or /intake for new work, before presenting a plan. "
        "Mapping and `peer` calls are allowed in plan mode. Inspecting, "
        "answering or cleaning up another feature's worker uses explicit `feature`/`dispatch_id` "
        "arguments and keeps the mapping. A peer message is not the owner's assignment; mapping "
        "from it needs the owner's ask and `switch=true`. "
        f"Until then captures go to {agent_display('raw', 'unassigned')}/. session {session_id}."
    )


def _blocked_block(root: Path, feature: dict[str, Any]) -> str:
    """The feature's `blocked` status, since when and why, any work since, and how to clear it,
    or "". The manager's own block never said its feature was blocked, so a manager worked
    for a day under a stale `blocked`."""
    try:
        from .. import blocked
        from .. import features as features_mod

        lines = blocked.session_lines(root, features_mod.resolve(root, str(feature["id"])))
    except Exception:  # noqa: BLE001 — a context line must not cost the session start
        return ""
    return "".join(f"\n{line}" for line in lines)


# ------------------------------------------------------------------ capture


def capture_hook(root: Path, payload: dict[str, Any]) -> str:
    kind = capture_mod.EVENT_TO_KIND.get(str(payload.get("hook_event_name") or ""))
    if kind is None:
        return ""
    try:
        capture_mod.capture(root, kind, payload)
    except AgentError:
        pass
    if kind == "session-end":
        from ..usage import job as usage_job

        if not payload.get("agent_id") and not payload.get("agent_type"):
            usage_job.trigger_quietly(root, "session-end")
        try:
            # a failed render is kept for doctor and must not skip releasing this session's leases
            status.render_quietly(root, source="session end")
            session_id = str(payload.get("session_id") or "")
            if session_id:
                from .. import leases as leases_mod

                leases_mod.release_all_for_session(root, session_id)
        except Exception:  # noqa: BLE001, S110 — a hook never raises; failing open is the contract
            pass
    return ""


# ------------------------------------------------------------------ dispatch-inbox


def dispatch_inbox(root: Path, payload: dict[str, Any]) -> str:
    """Drain pending worker and peer messages at the prompt boundary, when the session is about to
    think. Emit no context when nothing is pending.
    """
    if payload.get("agent_id") or payload.get("agent_type"):
        return ""
    from .. import dispatch as dispatch_mod
    from ..ops.peer_wake import DISPATCH_WAKE_TEXT, WAKE_TEXT

    blocks: list[str] = []
    session_id = str(payload.get("session_id") or "")
    prompt = payload.get("prompt")
    synthetic = prompt in (WAKE_TEXT, DISPATCH_WAKE_TEXT)
    inbox_read = False
    shown: set[str] = set()  # dispatches whose notice this turn renders; the fallback skips them
    if session_id:
        try:
            from .. import peers as peers_mod
            from . import notice_inbox

            # A Claude wake is one socket frame, not a queued prompt. When it lands mid-turn it
            # arrives as an in-turn message whose prompt is not WAKE_TEXT, so the reservation was
            # never acknowledged and every send for the next 30 minutes skipped the ring (one
            # reservation held seven messages until the owner typed).
            # Any Claude inbox read acknowledges; Codex keeps waiting for its queued wake prompt.
            # Either wake purpose drains both queues: they share one reservation.
            record = sessions.get(root, session_id) or {}
            acknowledge = synthetic or record.get("provider") != "codex"
            rows, consumed = notice_inbox.take(root, session_id, acknowledge_wake=acknowledge)
            inbox_read = True
            blocks += [b for b in (notice_inbox.block(consumed), peers_mod.block(rows)) if b]
            shown = {str(env.get("dispatch_id")) for env in consumed}
            from .notes_sync import evidence_lines

            blocks += evidence_lines(root, "\n".join(str(r.get("text") or "") for r in rows), session_id)
        except Exception:  # noqa: BLE001, S110 — an unreadable inbox must not cost the turn
            pass
    # A standing worker alert is injected on every turn, including a stale wake, and so is a
    # historical chat_pending. Neither makes the wake useful: both remain at the next real turn.
    if inbox_read and not blocks and synthetic:
        reason = "Peer wake already delivered." if prompt == WAKE_TEXT else "Dispatch wake already delivered."
        return json.dumps({"decision": "block", "reason": reason})
    try:
        worker_block = dispatch_mod.inbox_block(root, session_id=session_id, shown=shown)
    except Exception:  # noqa: BLE001 — notices already consumed above must still reach the turn
        worker_block = ""
    if worker_block:
        blocks.append(worker_block)
    stuck = _stuck_tasks_line(root, session_id) if session_id else ""
    if stuck:
        blocks.append(stuck)
    if not blocks:
        return ""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(blocks),
            }
        }
    )


def _stuck_tasks_line(root: Path, session_id: str) -> str:
    """Why this session's ⏳ may be stuck, from its transcript scan, or ""."""
    try:
        from .task_notices import stuck_line

        return stuck_line(sessions.get(root, session_id) or {})
    except Exception:  # noqa: BLE001 — a context line must not cost the turn
        return ""


# ------------------------------------------------------------------ render-after-plan-edit


def edited_paths(payload: dict[str, Any]) -> list[str]:
    """Every file an edit tool call names: Claude's Edit/Write `file_path`, or each path in a
    Codex `apply_patch` (added, updated, deleted, moved). Codex patches carry no `file_path`, so
    its plan edits never re-rendered."""
    from .patch_lease import patch_targets

    paths = [str(dig(payload, "tool_input", "file_path") or "")]
    if payload.get("tool_name") == "apply_patch":
        paths += patch_targets(str(dig(payload, "tool_input", "command") or ""))
    return [p for p in paths if p]


def is_plan_path(rel: str) -> bool:
    """Recognise feature plan paths at vault root or under initiatives (see ADR-0040)."""
    from ..feature_tree import FEATURE_DIR_RE, FEATURES_DIR

    parts = rel.split("/")
    return (
        len(parts) >= 3
        and parts[-1] == "plan.md"
        and parts[-3] == FEATURES_DIR
        and FEATURE_DIR_RE.match(parts[-2]) is not None
    )


def render_after_plan_edit(root: Path, payload: dict[str, Any]) -> str:
    from .notes_paths import notes_logical

    for raw in edited_paths(payload):
        rel = relative(root, raw)
        # In a linked vault `features/` resolves into the notes repo, so a plan edit never matched
        # and STATUS was never re-rendered after one: judge it by its vault path.
        rel = notes_logical(root, rel) or rel
        if is_plan_path(rel):
            # A failed render is kept for doctor and told to telemetry, never raised.
            status.render_quietly(root, source="plan edit")
            break
    return ""
