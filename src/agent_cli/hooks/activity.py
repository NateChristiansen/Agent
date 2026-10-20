"""Record manager activity from hook events and refresh `.agent/live.json`.

SessionStart and Stop mark idle; UserPromptSubmit marks working. Compaction and resume of a working
session preserve activity. A Stop blocked by an earlier group member stays working. Questions and
permissions mark waiting on the owner until a reply, tool completion or explicit resolution.
SessionEnd marks ended. Subagent events cannot replace manager activity; their permission prompts
affect only its tab.

Plain-text questions have no hook event, so `owner_question` records them explicitly. Tab labels
rank owner obligations above background work above activity (`obsidian_tabs.label_for`).

Background tasks outlive turns. Track their launch and completion events rather than infer them
from processes: MCP servers and the client also remain alive without being awaited. Bash, Monitor
and async subagents carry task ids. Stop scans the transcript for completion attachments that reach
no hook. Fresh SessionStart clears tasks; compaction keeps tasks that can still deliver notices.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import core, sessions, tab_drain, tab_reset
from ..records.narrow import as_dict, as_list, as_list_or_none, as_str, dict_list, dig
from .attention import (
    clear_question,
    heartbeat,
    is_wake,
    observe,
    owner_answered,
    question_texts,
    raise_question,
    sync_question_tool,
)
from .task_waits import PERMISSION_FIELD, reported_tasks, subagent_event, wait_count, waits_after
from .task_waits import pending_after as _pending_after

WAITING_TOOLS = ("AskUserQuestion", "ExitPlanMode")
PERMISSION_DEBOUNCE_S = 20


def _state_for(payload: dict[str, Any], current: str | None) -> str | None:
    event = str(payload.get("hook_event_name") or "")
    tool = str(payload.get("tool_name") or "")
    if event == "Stop" and payload.get("_stop_blocked"):
        # An earlier member of the Stop group blocked it, so the turn goes on.
        return "working"
    if event == "SessionStart":
        # Compaction and resume of a working session continue its turn; startup, clear and fork
        # start at rest.
        source = str(payload.get("source") or payload.get("session_reason") or "")
        if source == "compact" or (source == "resume" and current == "working"):
            return None
        return "idle"
    if event == "Stop":
        return "idle"
    if event == "UserPromptSubmit":
        if is_wake(payload):
            # The inbox may block a queued wake, and a blocked prompt has no Stop, so marking it
            # working would strand the tab until a later real turn. The combined prompt hook
            # calls this only after the inbox let the wake through and marks it so; that wake
            # starts a turn. The old separate wiring has no verdict to go on.
            return "working" if payload.get("_wake_accepted") else None
        return "working"
    if event == "PermissionRequest" or (
        event == "PreToolUse"
        and (tool in WAITING_TOOLS or tool.endswith(("request_user_input", "request_user_input_async")))
    ):
        return "waiting_on_you"
    if event == "PostToolUse":
        return "working" if current in ("waiting_on_you", "idle") else None
    if event == "SessionEnd":
        return "ended"
    return None


def set_activity(root: Path, session_id: str, state: str, *, show_after: str | None = None) -> None:
    """Persist a new activity state. The snapshot write's intent comes first, so a hook killed between
    this and its write leaves it standing. Plain working and idle
    changes get no watchdog, since they happen on most events and the next write publishes
    them; one that starts or ends an approval (`waiting_on_you`) does, through
    `sessions.update_record`."""
    from ..vault import live

    live.begin_intent(root)
    sessions.set_field(
        root,
        session_id,
        "activity",
        {"state": state, "at": core.iso(core.now()), **({"show_after": show_after} if show_after else {})},
    )


def clear_owner_question(root: Path, session_id: str) -> None:
    clear_question(root, session_id, "answered")


def _question_events(root: Path, session_id: str, record: dict[str, Any], payload: dict[str, Any]) -> None:
    """The question lifecycle's hook side (table in `attention`)."""
    event = str(payload.get("hook_event_name") or "")
    tool = str(payload.get("tool_name") or "")
    if record.get("owner_question_pending") and owner_answered(payload):
        if record.get("owner_question_route") == "async":
            clear_owner_question(root, session_id)
        elif (record.get("owner_question_route") or "manual") == "manual":
            sessions.update_record(
                root,
                session_id,
                lambda current: (
                    {
                        **current,
                        "owner_question_pending": False,
                        "owner_question_replied": True,
                    }
                    if current.get("owner_question_id") == record.get("owner_question_id")
                    and current.get("owner_question_pending")
                    else current
                ),
            )
    elif event == "Stop" and record.get("owner_question_replied") and not payload.get("_stop_blocked"):
        clear_owner_question(root, session_id)
    elif event == "PreToolUse" and sync_question_tool(tool):
        texts = question_texts(payload)
        raise_question(root, session_id, "sync", "\n".join(texts) or None)
        from .ask_guard import register

        for text in texts:
            register(root, session_id, "sync", text, payload)
    elif (
        event == "PostToolUse"
        and sync_question_tool(tool)
        and record.get("owner_question_pending")
        and record.get("owner_question_route") == "sync"
    ):
        clear_question(root, session_id, "answered")  # the prompt returned the owner's answer


def mark_resumed(root: Path, session_id: str, record: dict[str, Any]) -> None:
    """Every end from now on is ambiguous for this thread's tab, and a reset queued by an
    earlier end must not stay on the resumed tab. See
    `tab_reset`: bounded waits, and a tab a late paint clears is restored immediately after."""
    tab_reset.mark_resumed(root, session_id, record)


def activity(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("agent_id") or payload.get("agent_type"):
        subagent_event(root, payload)
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    record = sessions.get(root, session_id) or {}
    if record.get("dispatch_id"):
        return ""
    # The inbox address a peer wakes this session through. On Windows it is a named pipe with a
    # random name that only this environment knows. The address is not a secret; the token
    # that opens it stays in Claude Code's own key file.
    inbox = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") or ""
    if inbox and record.get("inbox") != inbox:
        sessions.set_field(root, session_id, "inbox", inbox)
    pending = as_dict(record.get("background_tasks"))
    after = _pending_after(payload, pending)
    if payload.get("hook_event_name") == "Stop":
        from .task_notices import close_finished

        closed = close_finished(root, session_id, record, payload, pending if after is None else after)
        after = closed if closed is not None else after
        reported = reported_tasks(
            payload,
            pending if after is None else after,
            [
                task_id
                for task_id in as_list(record.get("background_tasks_reported"))
                if isinstance(task_id, str)
            ],
        )
        after = reported if reported is not None else after
        listed = as_list_or_none(payload.get("background_tasks"))
        if listed is not None:
            sessions.set_field(
                root,
                session_id,
                "background_tasks_reported",
                [str(task["id"]) for task in dict_list(listed) if task.get("id")],
            )
        if "session_crons" in payload:
            sessions.set_field(
                root,
                session_id,
                "session_crons",
                {
                    str(c.get("id") or c.get("name") or i): c
                    for i, raw in enumerate(as_list(payload.get("session_crons")))
                    if (c := as_dict(raw))
                },
            )
    if (
        payload.get("hook_event_name") == "SessionStart"
        and str(payload.get("source") or payload.get("session_reason") or "") != "compact"
    ):
        sessions.set_field(root, session_id, "session_crons", {})
        sessions.set_field(root, session_id, "peer_waits", {})
        sessions.set_field(root, session_id, "background_tasks_reported", [])
    if after is not None:
        sessions.set_field(root, session_id, "background_tasks", after)
    _question_events(root, session_id, record, payload)
    waits = as_dict(record.get(PERMISSION_FIELD))
    if waits:
        event = str(payload.get("hook_event_name") or "")
        left = waits_after(event, waits, pending, pending if after is None else after)
        if left is not None:
            sessions.set_field(root, session_id, PERMISSION_FIELD, left)
    current = as_str(dig(record, "activity", "state"))
    state = _state_for(payload, current)
    observe(root, session_id, payload, current, state, provider=as_str(record.get("provider")) or None)
    resumed = bool(payload.get("_resume_claim_untitled"))
    tab_args: dict[str, Any] = {
        "claim_untitled": payload.get("hook_event_name") == "SessionStart" or resumed,
        "resume_display": str(payload.get("_resume_old_display") or ""),
    }
    if resumed:
        mark_resumed(root, session_id, record)
    show_after = None
    if payload.get("hook_event_name") == "PermissionRequest" and record.get("provider") == "codex":
        show_after = str(
            dig(record, "activity", "show_after")
            or core.iso(core.now() + timedelta(seconds=PERMISSION_DEBOUNCE_S))
        )
    if state is None or (state == current and (not show_after or dig(record, "activity", "show_after"))):
        # The label is not a function of the state alone: a worker acked, cancelled or abandoned
        # changes what the tab should read without changing this session's activity. Returning
        # here left the old label in place, which is how an idle manager kept an hourglass over a
        # worker the owner had already dealt with. Relabelling is idempotent
        # and Obsidian skips a tab that already reads what it should.
        if current and current != "ended":
            heartbeat(root, session_id, record)
        refresh_tab(root, session_id, **tab_args)
        return ""
    # Activity and session-start run concurrently. Record identity here too so a new or resumed
    # manager is not omitted from live views before session-start writes its pid.
    pid, known = sessions.resolve_identity()
    if (pid and record.get("pid") != pid) or (not known and not record.get("pid_unknown")):
        from ..session_liveness import set_identity

        set_identity(root, session_id, pid, known)
    set_activity(root, session_id, state, show_after=show_after)
    from ..vault import live

    live.write_quietly(root)
    refresh_tab(root, session_id, **tab_args)
    return ""


def codex_activity(root: Path, payload: dict[str, Any]) -> str:
    """Register a Codex manager, then use the same state transitions as Claude."""
    session_id = str(payload.get("session_id") or "")
    if not session_id or payload.get("agent_id") or payload.get("agent_type"):
        return ""
    record = sessions.get(root, session_id) or {}
    if record.get("dispatch_id"):
        return ""
    from .naming import desired_title

    base = desired_title(record, session_id)
    # Codex UUIDs share a timestamp prefix; the tail distinguishes concurrent sessions.
    suffix = session_id[-8:]
    name = f"{base}-codex-{suffix}" if base else f"mgr-codex-{suffix}"
    pid, known = sessions.resolve_identity()
    if record.get("name") != name:
        sessions.set_record(root, session_id, name=name)
    if record.get("pid") != pid or bool(record.get("pid_unknown")) == known:
        from ..session_liveness import set_identity

        # Not set_record(pid=None): that keeps the old pid, which was the daemon's.
        set_identity(root, session_id, pid, known)
    if record.get("provider") != "codex":
        sessions.set_field(root, session_id, "provider", "codex")
    event = str(payload.get("hook_event_name") or "")
    source = str(payload.get("source") or payload.get("session_reason") or "")
    if event == "SessionStart" and source == "resume":
        # The old tab name points at the closed leaf. Claim the active resumed terminal.
        old_display = str(record.get("tab_display_name") or "")
        for field in ("tab_display_name", "tab_name", "tab_label"):
            sessions.set_field(root, session_id, field, None)
        payload = {**payload, "_resume_claim_untitled": True, "_resume_old_display": old_display}
    tool = str(payload.get("tool_name") or "")
    if event in ("SessionStart", "UserPromptSubmit", "Stop"):
        day = core.today().isoformat()
        if event == "Stop":
            from ..prices import PRICED_AT, price_rollout

            transcript = payload.get("transcript_path")
            amount = price_rollout(Path(transcript)) if isinstance(transcript, str) and transcript else None
            if amount is None:
                sessions.mark_cost_unknown(
                    root, session_id, day, force=record.get("cost_source") == "price-estimate"
                )
            else:
                sessions.record_cost(
                    root, session_id, amount, day, source="price-estimate", priced_at=PRICED_AT
                )
        else:
            sessions.mark_cost_unknown(root, session_id, day)
    if event == "PostToolUse" and tool.endswith("request_user_input_async"):
        # The async question tool returns before the owner answers. Its PostToolUse and Stop
        # must not turn the tab back to working or idle while the question remains open; the
        # owner's next prompt ends it, through `activity` like every other route.
        raise_question(root, session_id, "async")
    return activity(root, payload)


CLAIM_FIELD = "tab_claim"
CLAIM_BACKOFF_S = 10.0
CLAIM_BACKOFF_MAX_S = 600.0


def claim_due(state: dict[str, Any], marker: str) -> bool:
    """Retry a failed first claim after 10 s, doubling to 10 minutes to bound hook delays. A new
    ribbon marker identifies a fresh launch and retries immediately. Failure reasons stay in
    `.agent/tabs/claim-failures.jsonl`.
    """
    if not state:
        return True
    if marker and marker != state.get("marker"):
        return True
    return time.time() >= float(state.get("next_at") or 0)


def claim_failed(state: dict[str, Any], marker: str) -> dict[str, Any]:
    # A new ribbon marker is a fresh launch: count from zero rather than resume the old wait.
    fresh = bool(marker) and marker != state.get("marker")
    previous = 0 if fresh else int(state.get("attempts") or 0)
    attempts = previous + 1
    # Clamp the exponent before multiplying: 2 ** 10000 overflows a float long before the cap.
    wait = min(CLAIM_BACKOFF_S * 2 ** min(attempts - 1, 16), CLAIM_BACKOFF_MAX_S)
    return {
        "attempts": attempts,
        "next_at": time.time() + wait,
        "marker": marker,
        "why": f"see {core.agent_display('tabs', 'claim-failures.jsonl')}",
    }


def name_in(label: str) -> str:
    """The session name inside a stored tab label, or "".

    `obsidian_tabs.label_for` always emits "<glyph> [+N ]<name>" and never an empty string, so
    the name is whatever follows the glyph and any count. This is the only record of what a tab
    was called before a rename, for sessions whose `tab_name` predates it being tracked.
    """
    from ..obsidian_tabs import label_name

    return label_name(label)


def _relabel_by_suffix(root: Path, session_id: str, label: str) -> None:
    """Repaint only a Codex tab carrying this session's exact suffix, independently of first-claim
    backoff. Renaming removes the launch marker, so suffix matching keeps resumed tabs reachable
    without claiming unrelated tabs.
    """
    from .. import obsidian_tabs
    from ..tab_drain import request

    request(
        root,
        "manager",
        session_id,
        "",
        label,
        generated=False,
        token=obsidian_tabs.manager_identity(session_id, "codex"),
    )


RESUMED_FIELD = tab_drain.RESUMED_FIELD
RESET_SKIPPED_FIELD = "tab_reset_skipped"
AMBIGUOUS_END = "end after a resume of this thread; nothing shows which process sent it"


def _clear_ended_tab(root: Path, session_id: str, record: dict[str, Any]) -> None:
    """Reset an ended Codex tab only when exactly one tab matches its full display. Preserve the
    display on the record for resume matching.

    Codex reuses thread ids and sends delayed end events with no process or generation identity.
    Hooks share a daemon, so process lookup cannot distinguish an old end from a resumed session's
    real quit. Resumed threads therefore keep their tabs and record why.
    """
    display = str(record.get("tab_display_name") or "")
    if not display:  # never claimed, or resumed and not claimed again: no tab carries it
        return
    if record.get(RESUMED_FIELD):
        if record.get(RESET_SKIPPED_FIELD) != AMBIGUOUS_END:
            sessions.set_field(root, session_id, RESET_SKIPPED_FIELD, AMBIGUOUS_END)
        return
    from ..tab_drain import request

    request(root, "manager", session_id, display, "", generated=False, reset=True)


def desired_label(
    root: Path,
    session_id: str,
    record: dict[str, Any],
    *,
    dispatches: list[dict[str, Any]] | None = None,
    owed: dict[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    """`(name, display, label)` the session's tab should carry now, from its current state, or
    None while it has no name. Also what restores a resumed tab a late reset cleared.
    `live.build` passes the `dispatches` and `owed` it already derived, for every manager."""
    from .. import dispatch as dispatch_mod
    from .. import obsidian_tabs
    from ..vault import live as live_mod
    from ..vault.managers import live_manager_records
    from .naming import desired_title, live_name

    if dispatches is None:
        dispatches = dispatch_mod.load_all(root)
    mine = [d for d in dispatches if d.get("launched_by_session") == session_id]
    running = [str(d["id"]) for d in mine if live_mod.running_dispatch(d)]
    # A finished worker not yet handled is the manager's next step, not the owner's: read, land
    # or ack it. As ✋ it trained the owner to inspect routine completions as if blocked on them,
    # and as ⏳ it read as still running. It is now a
    # dashboard count, from the one derivation of what is owed (`live.obligations`).
    if owed is None:
        pairs = live_manager_records(root)
        ended = dig(record, "activity", "state") == "ended"
        if not ended and session_id not in {str(r.get("session_id")) for r, _ in pairs}:
            # This hook is the session's own event, so it is live even before its record shows it.
            pairs.append(({**record, "session_id": session_id}, {}))
        owed = live_mod.obligations(root, dispatches=dispatches, pairs=pairs)
    manager_action = len(as_list(as_dict(owed["manager_action"]).get(session_id)))
    needs_owner: list[str] = []
    if record.get("owner_question_pending"):
        needs_owner.append("question")
    # A subagent's permission prompt waits on the owner as much as the manager's own.
    needs_owner.extend(f"permission:{agent}" for agent in as_dict(record.get(PERMISSION_FIELD)))
    # Track background tasks from events so their outstanding work keeps the tab waiting.
    background = wait_count(record)
    # Match the client-visible name if the stored copy is missing or owner-selected, so permission
    # prompts still find the tab.
    live, source = live_name(record.get("pid"))
    name = record.get("name") or live
    if not name:
        return None
    provider = str(record.get("provider") or "claude")
    generated = (
        provider == "codex"
        or name == desired_title(record, session_id)
        or name == desired_title(record, session_id, taken={name})  # a suffixed second manager
        or (source == "hook" and name.startswith("mgr-"))
    )
    display = obsidian_tabs.manager_display_name(name, session_id, provider, generated=generated)
    label = obsidian_tabs.label_for(
        display, as_str(dig(record, "activity", "state")), running, background, needs_owner, manager_action
    )
    return name, display, label


def refresh_tab(
    root: Path, session_id: str, *, claim_untitled: bool = False, resume_display: str = ""
) -> None:
    """Relabel a manager tab from owner obligations, workers and background tasks. Called on hook
    events and worker completion; never raises.

    Compare the label with `live.json` before rebuilding: acknowledgements and questions can change
    it without changing activity. The plugin paints the snapshot; `tab_drain` pushes only while its
    heartbeat is unhealthy.
    """
    try:
        from .. import obsidian_tabs

        record = sessions.get(root, session_id) or {}
        if record.get("provider") == "codex" and dig(record, "activity", "state") == "ended":
            _clear_ended_tab(root, session_id, record)
            return
        desired = desired_label(root, session_id, record)
        if desired is None:
            # `relabel` would refuse this anyway; returning here keeps `tab_label` unwritten so
            # the first relabel after a name arrives is not skipped as unchanged.
            return
        name, display, label = desired
        from ..vault import live

        timing = live.tab_timing(record, label)
        live.write_for_tab(root, session_id, label, timing=timing)
        snapshot_label = label
        after = timing.get("show_after")
        if after and core.now() < datetime.fromisoformat(after):
            label = str(timing["before_label"])
        provider = str(record.get("provider") or "claude")
        # Every event recomputes the desired label because a worker ack can change it without
        # changing activity. The shared drainer deduplicates requests and limits *aggregate*
        # Obsidian evals across Claude, Codex, and worker watch tabs.
        old_name = _old_name(record)
        # Not for Codex: its hooks run in a shared app-server daemon, which keeps the environment
        # of whichever tab started it, so every later Codex session claimed with that first tab's
        # marker and found no tab (fresh Windows clone). Codex's marker comes
        # from its own TUI process below.
        ribbon_marker = "" if provider == "codex" else os.environ.get("AGENT_RIBBON_LAUNCH") or ""
        ribbon_client = ribbon_marker.partition(":")[0]
        if not old_name and (provider == "codex" or ribbon_client == "claude"):
            # A new Codex tab has no harness name until its first claim. A ribbon tab has a
            # provisional title and carries the launched client in its Terminal profile.
            # Claim only the active matching leaf; later updates use its unique session name.
            # SessionStart's one-shot untitled claim is its own attempt; only the ordinary claim
            # that follows on every later event is spaced out.
            if provider == "codex":
                if not claim_untitled:  # a new, untitled tab has no suffix yet
                    _relabel_by_suffix(root, session_id, label)
                # Codex hooks run in its managed app-server daemon and never see the marker.
                # Recover it from the TUI process that created this thread.
                from ..codex_tab_link import marker_for

                ribbon_marker = marker_for(session_id)
                ribbon_client = ribbon_marker.partition(":")[0]
                if not ribbon_marker:
                    # No proof of which tab is this thread's. Claiming the active untitled leaf
                    # instead could take another Codex session's tab, since every Codex hook
                    # runs in the same daemon, so this tab stays unclaimed.
                    # This includes a resume into a new tab: its old thread has no
                    # marker, so it is deferred rather than guessed.
                    return
            claim_state = {} if claim_untitled else as_dict(record.get(CLAIM_FIELD))
            if not claim_due(claim_state, ribbon_marker):
                return
            provisional = {
                "codex": obsidian_tabs.CODEX_STARTING_TITLE,
                "claude": obsidian_tabs.CLAUDE_STARTING_TITLE,
            }.get(ribbon_client or "", "")
            claimed = obsidian_tabs.claim_active(
                name,
                label,
                allow_untitled=claim_untitled and provider == "codex",
                resume_display=resume_display if provider == "codex" else "",
                provisional_title=provisional,
                launch_marker=ribbon_marker,
            )
            if not claimed:
                if not claim_untitled:
                    failed = claim_failed(claim_state, ribbon_marker)
                    sessions.set_field(root, session_id, CLAIM_FIELD, failed)
                return
            if record.get(CLAIM_FIELD):
                sessions.set_field(root, session_id, CLAIM_FIELD, None)
            sessions.set_field(root, session_id, "tab_display_name", display)
            sessions.set_field(root, session_id, "tab_label", label)
            return
        from ..tab_drain import request

        target = tab_target(session_id, record, name)
        request(
            root,
            "manager",
            session_id,
            target["match"],
            snapshot_label,
            token=target["token"],
            generated_name=target["generated_name"],
            **timing,
        )
    except Exception as exc:  # noqa: BLE001 — a label is a courtesy
        sys.stderr.write(f"note: tab relabel failed ({exc})\n")


def _old_name(record: dict[str, Any]) -> str:
    """The name the session's tab was last painted with, or ""."""
    return (
        str(record.get("tab_display_name") or "")
        or str(record.get("tab_name") or "")  # older state
        or name_in(str(record.get("tab_label") or ""))
    )


def tab_target(session_id: str, record: dict[str, Any], name: str) -> dict[str, Any]:
    """How a painter finds this manager's tab, the drainer and the plugin alike: by `match` (the
    name it was last painted with, else the session's name) or `generated_name` in the title it
    carries or, with `generated`, shows; failing both, by the `· cl 834e` suffix `token`. A
    Codex tab not yet claimed has no harness name in it, so only its suffix can find it
    (`_relabel_by_suffix`)."""
    from .. import obsidian_tabs

    provider = str(record.get("provider") or "claude")
    token = obsidian_tabs.manager_identity(session_id, provider)
    old_name = _old_name(record)
    if not old_name and provider == "codex":
        return {"match": "", "token": token, "generated_name": "", "generated": False}
    return {"match": old_name or name, "token": token, "generated_name": name, "generated": True}
