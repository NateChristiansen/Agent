"""Close features and return the remaining dispatch and cleanup obligations. The `/close` workflow
runs retro first; closure itself does not require a retro record.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .. import capture as capture_mod
from .. import dispatch as dispatch_mod
from .. import features as features_mod
from .. import log as log_mod
from ..core import AgentError
from ..records.narrow import as_dict, as_list
from ._shared import Op, render_views
from .features import _feature_dict  # pyright: ignore[reportPrivateUsage]


def _clear_chat_pending(root: Path, feature_id: int) -> None:
    """A closed feature's returns wait on nobody, so the per-prompt fallback stops
    listing them. Best effort: the close already happened."""
    from ..dispatch.state import save
    from ..dispatch.verification_gate import record_guard

    for d in dispatch_mod.load_all(root):
        if d.get("feature") != feature_id or not d.get("chat_pending"):
            continue
        try:
            with record_guard(root, str(d["id"])) as current:
                current["chat_pending"] = False
                save(root, current)
        except (AgentError, OSError) as exc:
            sys.stderr.write(f"note: chat-pending not cleared on {d['id']} ({exc})\n")


def feature_close(
    root: Path,
    *,
    feature: str,
    status: str = "done",
    note: str | None = None,
    session_id: str | None = None,
) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    return close_record(root, f, status=status, note=note, session_id=session_id)


def close_record(
    root: Path,
    f: features_mod.Feature,
    *,
    status: str,
    note: str | None = None,
    session_id: str | None = None,
    result: str | None = None,
) -> Op:
    """Close a feature or a request, then run the safe cleanup both share: worktrees, chat
    pending, transcripts. A request is refused before anything changes while its dispatches owe
    work (`requests.refuse_unfinished`), and needs no retro and no journal step. `result` is a
    request's delivered result, written with the status."""
    if status not in features_mod.TERMINAL_STATUSES:
        raise AgentError(f"close status must be one of {', '.join(features_mod.TERMINAL_STATUSES)}")
    if f.status in features_mod.TERMINAL_STATUSES:
        raise AgentError(f"{f.label.lower()} is already {f.status}")
    if result is not None:
        from .. import requests as requests_mod

        f.body = requests_mod.with_result(f.body, result)
    # Before the closing note is logged; `features.close` checks again.
    features_mod.refuse_terminal(root, f, status)
    if f.is_request:
        retro = "not required"
    else:
        retro = "present" if (f.dir / "retro.md").exists() else "missing"
    # Closed features leave Workers view; clear their unanswered dispatch questions so no hidden
    # hand remains.
    left_open = [
        d["id"]
        for d in dispatch_mod.load_all(root)
        if d.get("feature") == f.id
        and dispatch_mod.is_live(d)
        and d.get("state") not in ("queued", "running")
    ]
    if note:
        log_mod.append(f, "decision", note)
    features_mod.close(root, f, status)
    _clear_chat_pending(root, f.id)
    from ..telemetry import attention as events

    events.emit(root, "feature.close", session=session_id, feature=f.number, status=status, retro=retro)
    render_views(root)
    data = _feature_dict(root, f)
    # Run worktree cleanup in shared ops so CLI and MCP closure agree. Keep branches, and report
    # dirty worktrees rather than remove them (see ADR-0005).
    try:
        cleanup = dispatch_mod.cleanup(root, feature=str(data["id"]), force=False, session_id=session_id)
        data["worktrees_removed"] = as_list(cleanup.get("removed"))
        data["worktrees_kept"] = as_list(cleanup.get("skipped"))
    except AgentError as exc:
        data["worktrees_removed"] = []
        data["worktrees_kept"] = []
        data["cleanup_error"] = exc.message
    from .. import tidy

    try:
        managers = tidy.run(
            root, apply=True, feature=f.id, delete_branches=False, manager_only=True, session_id=session_id
        )
        data["worktrees_removed"].extend(managers["removed"])
        data["worktrees_kept"].extend(
            {"worktree": row["path"], "reason": "; ".join(row["holds"])}
            for row in managers["trees"]
            if row["holds"]
        )
    except (AgentError, OSError) as exc:
        data["manager_cleanup_error"] = str(exc)
    # Copy every transcript this feature's .agent/raw/ captures point at, before Claude Code's
    # cleanupPeriodDays (30 days by default) sweeps ~/.claude/projects out from under a later
    # retro (plan 3.5). A copy failure (permissions, disk full) must not make the close look
    # like it failed. The close already happened.
    try:
        transcripts = capture_mod.preserve_transcripts(root, f)
    except OSError as exc:
        sys.stderr.write(f"note: feature closed, but transcript preservation failed: {exc}\n")
        transcripts = []
    data["transcripts_preserved"] = len(transcripts)
    if f.is_request:
        summary = f"{'completed' if status == 'done' else 'closed'} {f.label} {f.slug} as {status}"
    else:
        summary = f"closed {f.number} {f.slug} as {status}"
    if data["worktrees_removed"]:
        summary += f"; removed {len(data['worktrees_removed'])} worktree(s)"
    if data["worktrees_kept"]:
        historical = [
            row
            for row in as_list(data["worktrees_kept"])
            if as_dict(row).get("reason")
            and all(
                reason.startswith(("live session ", "process cwd ", "lease "))
                for reason in str(as_dict(row)["reason"]).split("; ")
            )
        ]
        unresolved = [row for row in data["worktrees_kept"] if row not in historical]
        data["worktrees_kept_unresolved"] = unresolved
        data["worktrees_kept_historical"] = historical
        if unresolved:
            summary += f"; kept: unresolved work ({len(unresolved)} worktree(s))"
        if historical:
            summary += (
                "; kept: historical checkout, safe once branch resolved and holder exits "
                f"({len(historical)} worktree(s))"
            )
    if transcripts:
        summary += f"; preserved {len(transcripts)} transcript(s)"
    data["retro"] = retro
    if retro == "missing":
        summary += f"\nMissing retro: {f.number} — run /retro {f.number}."
    data["dispatches_left_open"] = left_open
    if left_open:
        summary += (
            f"\nwarning: {len(left_open)} dispatch(es) were still waiting on you and left the Workers "
            f"view: {', '.join(left_open)}. Ack or abandon them if that was not intended."
        )
    # Closing adds a journal milestone and instruction. Journal writing remains separate so its
    # failure cannot make a completed close fail.
    from ..journal import is_writer

    if not f.is_request and is_writer(root):
        data["next"] = "/journal today"
        summary += "\nNext: write today's journal (`/journal today`)."
    return Op(data, summary)
