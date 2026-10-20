"""Terminal-transition notifications (chat, and macOS on a Mac) and the inbox blocks the
session-start and UserPromptSubmit hooks print."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import sessions
from ..core import AgentError, iso, now
from ..records.dispatch import result_of
from ..records.narrow import as_dict, dig, str_list
from .state import (
    CLOSED_STATES,
    DEFAULT_NOTIFY,
    display_summary,
    load_all,
    one_line,
    save,
)

# ------------------------------------------------------------------ notify (A6)


def default_channels() -> list[str]:
    """`chat`, plus `macos` on a Mac: off a Mac it only logged "skipped: osascript not found"
    on every return."""
    return [c for c in DEFAULT_NOTIFY if c != "macos" or sys.platform == "darwin"]


NOTIFY_ENV = "AGENT_NOTIFY"  # "off" silences macos (chat_pending still records)


def notifications_enabled() -> bool:
    return os.environ.get(NOTIFY_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def _macos_notify(title: str, text: str) -> str:
    if os.environ.get("AGENT_NOW"):
        return "skipped: test clock (AGENT_NOW set)"
    if shutil.which("osascript") is None:
        return "skipped: osascript not found"
    # ensure_ascii=False: AppleScript has no \uXXXX escape. A worker summary with an em
    # dash or a curly quote made osascript fail with "Expected '\"' but found unknown token".
    script = f"display notification {json.dumps(text, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    return "sent" if proc.returncode == 0 else f"failed: {proc.stderr.strip()}"


def notify(root: Path, d: dict[str, Any], event: str) -> dict[str, str]:
    """Deliver terminal notifications through chat_pending inbox blocks and macOS osascript. Skip
    unsupported channels retained on older records.
    """
    summary = display_summary(one_line(result_of(d).get("summary") or d.get("error") or ""))
    title = f"dispatch {d['id']} {event}"
    text = f"{d['short']}: {summary}" if summary else d["short"]
    results: dict[str, str] = {}
    enabled = notifications_enabled()
    for channel in str_list(d.get("notify")):
        if channel == "chat" and d.get("state") in CLOSED_STATES:
            # A cancel or abandon closed it: nothing waits on its return.
            results["chat"] = "skipped: closed"
        elif channel == "chat":
            d["chat_pending"] = True
            results["chat"] = "pending"
        elif not enabled:
            results[channel] = f"skipped: {NOTIFY_ENV}=off"
        elif channel == "macos":
            results["macos"] = _macos_notify(title, text)
        else:
            results[channel] = "skipped: retired channel"
    from .verification_gate import record_guard

    # Delivery can overlap notice consumption or a new attempt. Merge only its audit fields.
    try:
        with record_guard(root, d["id"]) as current:
            if (
                current["attempt"] == d["attempt"]
                and current["state"] == d["state"]
                and current.get("record_revision") == d.get("record_revision")
                and results.get("chat") == "pending"
            ):
                current["chat_pending"] = True
            current.setdefault("notify_log", []).append(
                {"ts": iso(now()), "event": event, "results": results}
            )
            stash = as_dict(d.get("verification_telemetry"))
            latest = as_dict(current.get("verification_telemetry"))
            if current["attempt"] == d["attempt"] and stash.get("event_id") == latest.get("event_id"):
                if stash.get("recorded"):
                    latest["recorded"] = stash["recorded"]
            save(root, current)
    except (AgentError, OSError) as exc:
        sys.stderr.write(f"note: notification audit not saved ({type(exc).__name__})\n")
    from .. import obsidian_tabs

    # the worker's own watch tab reads ✋ when it needs the owner
    obsidian_tabs.relabel_worker(root, str(d["id"]), d.get("state"))
    if d.get("launched_by_session"):
        # the launching manager's tab stops reading ⏳ once its worker is done
        from ..hooks.activity import refresh_tab

        refresh_tab(root, str(d["launched_by_session"]))
    return results


# ------------------------------------------------------------------ inbox blocks


def _visible_dispatches(
    root: Path, session_id: str | None, records: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """A mapped manager sees its feature and dispatches it launched; unmapped sees all."""
    records = load_all(root) if records is None else records
    if not session_id:
        return records
    raw_id = dig(sessions.get(root, session_id), "feature", "id")
    if raw_id is None:
        return records
    feature_id = str(raw_id)
    return [
        d
        for d in records
        if str(d.get("feature")) == feature_id or d.get("launched_by_session") == session_id
    ]


def pending_block(root: Path, *, session_id: str | None = None, compact: bool = False) -> str:
    """Text for session-start, from `live.obligations`: the workers waiting on
    the owner, this session's own manager actions with their ages, orphaned work it can see,
    and what is still running. A mapped manager sees its feature and what it launched; an
    unmapped one sees all. Empty when there is nothing."""
    from ..vault import live as live_mod
    from ..vault.managers import live_manager_records

    every = load_all(root)
    records = _visible_dispatches(root, session_id, every)
    visible = {str(d["id"]): d for d in records}
    running = [d for d in records if d.get("state") in ("queued", "running")]
    pairs = live_manager_records(root)
    if session_id and session_id not in {str(r.get("session_id")) for r, _ in pairs}:
        # Session start runs before the session's own record proves it live.
        pairs.append(({**(sessions.get(root, session_id) or {}), "session_id": session_id}, {}))
    owed = live_mod.obligations(root, dispatches=every, pairs=pairs)
    owner = [
        (visible[str(i["id"])], i)
        for i in owed["needs_you"]
        if i.get("kind") == "worker" and str(i.get("id")) in visible
    ]
    mine = [
        (visible[str(i["id"])], i)
        for i in owed["manager_action"].get(session_id or "", [])
        if str(i.get("id")) in visible
    ]
    orphaned = [
        (row, [visible[i] for i in str_list(row.get("ids")) if i in visible])
        for row in owed["needs_you"]
        if row.get("kind") == "orphaned"
    ]
    orphaned = [(row, ds) for row, ds in orphaned if ds]
    if not session_id:  # no session to owe anything: every manager action is listed as the owner's
        mine = [
            (visible[str(i["id"])], i)
            for items in owed["manager_action"].values()
            for i in items
            if str(i.get("id")) in visible
        ]
    if compact:
        return _render_compact(owner, mine, orphaned, running)
    return _render_owed(owner, mine, orphaned) + _render([], running, lead=bool(owner or mine or orphaned))


COMPACT_ITEMS = 3


def _brief(d: dict[str, Any], owes: str = "") -> str:
    summary = one_line(result_of(d).get("summary") or d.get("error") or "")
    extra = f"; {owes}" if owes else ""
    return f"  {d['id']} {d['short']} — {d['state']}{extra}: {display_summary(summary, 70)}"


def _render_compact(
    owner: list[tuple[dict[str, Any], dict[str, Any]]],
    mine: list[tuple[dict[str, Any], dict[str, Any]]],
    orphaned: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    running: list[dict[str, Any]],
) -> str:
    """Counts and the first few owed items, one line each. The rest is in the full-list file,
    which `pending_block` without `compact` renders."""
    more = "  … {k} more in the full-list file"
    lines: list[str] = []
    if owner:
        lines.append(_WAITING.format(n=len(owner)))
        lines += [_brief(d) for d, _ in owner[:COMPACT_ITEMS]]
        if len(owner) > COMPACT_ITEMS:
            lines.append(more.format(k=len(owner) - COMPACT_ITEMS))
    if mine:
        lines.append(
            f"manager actions for this session ({len(mine)}), oldest first: dispatch_read show, then dispatch consume"
        )
        from ..vault.live import age_s

        for d, item in mine[:COMPACT_ITEMS]:
            owes = ", ".join(str_list(item.get("reasons")))
            lines.append(_brief(d, f"{owes}; {_age(age_s(item.get('since')))}"))
        if len(mine) > COMPACT_ITEMS:
            lines.append(more.format(k=len(mine) - COMPACT_ITEMS))
    if orphaned:
        total = sum(len(ds) for _, ds in orphaned)
        lines.append(
            f"orphaned dispatches ({total} on {len(orphaned)} feature(s)): their manager has ended; "
            "map this session with feature_use to adopt them"
        )
        for row, ds in orphaned[:COMPACT_ITEMS]:
            ids = ", ".join(str(d["id"]) for d in ds[:4]) + (" …" if len(ds) > 4 else "")
            lines.append(f"  feature {row.get('feature')} ({len(ds)}): {ids}")
        if len(orphaned) > COMPACT_ITEMS:
            lines.append(more.format(k=len(orphaned) - COMPACT_ITEMS))
    if running:
        names = ", ".join(f"{d['id']} {d['short']}" for d in running[:6]) + (" …" if len(running) > 6 else "")
        lines.append(f"in flight ({len(running)}): {names}")
    return "\n".join(lines)


def _age(seconds: object) -> str:
    if not isinstance(seconds, int):
        return "age unknown"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m old"
    if minutes < 48 * 60:
        return f"{minutes // 60}h{minutes % 60:02d}m old"
    return f"{minutes // (24 * 60)}d old"


def _render_owed(
    owner: list[tuple[dict[str, Any], dict[str, Any]]],
    mine: list[tuple[dict[str, Any], dict[str, Any]]],
    orphaned: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> str:
    lines: list[str] = []
    if owner:
        lines.append(_WAITING.format(n=len(owner)))  # questions and blockers, read or not
        for d, _ in owner:
            lines += _dispatch_lines(d)
    if mine:
        lines.append(
            f"manager actions for this session ({len(mine)}), oldest first: read each with "
            "dispatch_read show, then dispatch consume; land, ack or abandon a finished branch"
        )
        from ..vault.live import age_s

        for d, item in mine:
            owes = ", ".join(str_list(item.get("reasons")))
            lines += _dispatch_lines(d, f"{owes}; {_age(age_s(item.get('since')))}")  # counted now
    for row, ds in orphaned:
        resume = as_dict(row.get("resume"))
        how = (
            f"resume its manager: cd {resume['cwd']} && {resume['command']}"
            if resume.get("command")
            else "open a manager from the ribbon"
        )
        lines.append(
            f"orphaned on feature {row.get('feature')} ({len(ds)}): its manager has ended. {how}, "
            "or map this session to the feature with feature_use to adopt them"
        )
        for d in ds:
            lines += _dispatch_lines(d)
    return "\n".join(lines)


def _dispatch_lines(d: dict[str, Any], owes: str = "") -> list[str]:
    summary = one_line(result_of(d).get("summary") or d.get("error") or "")
    cost = f"${float(d.get('cost_usd') or 0):.2f}"
    extra = f"; {owes}" if owes else ""
    hint = _HINTS.get(str(d.get("state")), "").format(worktree=d.get("worktree"))
    out = [f"  {d['id']} {d['short']} — {d['state']} ({cost}{extra}): {display_summary(summary, 120)}"]
    if hint:
        out.append(f"      {hint}  → dispatch show {d['id']}")
    if d.get("session_id"):
        out.append(
            f"      the owner can open it directly: cd {d['worktree']} && claude --resume {d['session_id']}"
        )
    return out


_WAITING = "dispatches waiting on you ({n}) — read each with the dispatch tool, action show:"
_HINTS = {
    "question": "answer: dispatch tool, action resume, with `answer`",
    "blocked": "resume, answer, or abandon; worktree kept at {worktree}",
    "failed": "see the error, then resume or abandon; worktree kept at {worktree}",
    "done": "read the handoff; push/PR is a separate step; then dispatch tool, action ack",
}


def _render(pending: list[dict[str, Any]], running: list[dict[str, Any]], *, lead: bool = False) -> str:
    """`pending` and `running` as text. `lead` says text comes before it, so it starts on a
    new line."""
    if not pending and not running:
        return ""
    lines: list[str] = [""] if lead else []
    if pending:
        lines.append(_WAITING.format(n=len(pending)))
        for d in pending:
            lines += _dispatch_lines(d)
    for d in running:
        since = str(d.get("started") or "")[11:16]
        lines.append(f"in flight: {d['id']} {d['short']} ({d['state']} since {since})")
        if d.get("session_id"):
            lines.append(f"      watch: dispatch tool, action watch, dispatch_id {d['id']}")
            lines.append(f"      or take it over: cd {d['worktree']} && claude --resume {d['session_id']}")
    return "\n".join(lines)


# A notice in one of these delivery states sits in its recipient's notice store, which the same
# prompt hook drains (`hooks.notice_inbox`). One consumed by a hook was drained from it. An
# intent shown to its caller at creation (`show-consumed`, a launcher's own reverify) keeps the
# per-prompt fallback, as `notice_producers.reverify_raised` intends.
_REACHED = ("inbox-accepted", "wake-accepted", "wake-failed")


def noticed(root: Path, d: dict[str, Any], session_id: str) -> bool:
    """Whether this dispatch's current typed notice to `session_id` is still queued in its notice
    store: the next drain shows it, so the per-prompt list need not. A notice a
    hook already consumed does not count: consumption is saved before the notice is rendered, so
    a hook that died in between lost it, and the list is then the only way the manager learns of
    the return. A hook records its consumption in the recipient's notice
    store, not on this source record, so that store decides (round 2)."""
    from .notice_delivery import consumed_as
    from .notices import GENERATION_FIELD, OUTBOX_FIELD

    for nid, value in as_dict(d.get(OUTBOX_FIELD)).items():
        entry = as_dict(value)
        if entry.get("generation") != d.get(GENERATION_FIELD):
            continue
        if dig(entry, "envelope", "recipient") != session_id:
            continue
        if entry.get("delivery") not in _REACHED or entry.get("consumption") not in (None, "none"):
            continue
        try:
            queued = consumed_as(root, session_id, str(nid)) is None
        except Exception:  # noqa: BLE001 — an unreadable store means list it, not hide it
            queued = False
        if queued:
            return True  # still queued: the next drain shows it
    return False


def inbox_block(root: Path, *, session_id: str | None = None, shown: set[str] | None = None) -> str:
    """UserPromptSubmit fallback: a return waiting (`chat_pending`) that this turn's notices did
    not show (`shown`, dispatch ids) and that is not still queued as a notice. That covers no
    route, a delivery that fell back, a mapped manager who did not launch it, and a notice whose
    rendering was lost after it was consumed. A return shown as a notice this turn is not listed
    again; one still unhandled at a later turn is."""
    from ..records.narrow import as_feature_id
    from .state import CLOSED_STATES, closed_feature_ids, is_live, is_probe
    from .verdicts import has_verdict, is_read

    closed = closed_feature_ids(root)
    waiting = [
        d
        for d in _visible_dispatches(root, session_id)
        if d.get("chat_pending")
        and is_live(d, closed)
        and not is_probe(d)
        and d.get("state") not in CLOSED_STATES
        and as_feature_id(d.get("feature")) not in closed
        and (not has_verdict(d) or not is_read(d))
        and str(d.get("id")) not in (shown or set())
        and not (session_id and noticed(root, d, session_id))
    ]
    return _render(waiting, [])
