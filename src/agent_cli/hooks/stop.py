"""Turn-end and nudge hooks: `lease-yield` (the turn-end stamp), `journal-pending`
(milestones landed today with no note yet) and `thrash-guard` (the session-local
repetition detector). Also `note_hook_denial`, called by `run()` for every hook's output, which
counts this harness's own PreToolUse denials toward the thrash threshold (`PermissionDenied` never
fires for those, so nothing else would).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .. import sessions
from ..core import agent_path
from ..records.narrow import as_dict, as_int, dict_list
from .paths import slashes

# ------------------------------------------------------------------ lease-yield


def lease_yield(root: Path, payload: dict[str, Any]) -> str:
    """Stop: stamp the turn end and ring the peer messages this session deferred once they are due.

    The stamp (`leases.HOLDER_STOPPED_KEY`) makes every lease this session touched before it
    `resting`: the holder sits at a prompt, so another session's next edit takes the lease over
    (`leases.status`). That is how a lease yields at turn end. Two sessions deadlocked on
    2026-09-16 holding the lease each other needed; neither one blocks past its turn now. The
    holder's next edit in the area takes the lease back, or is told who has it now.

    A Stop an earlier group member blocked (`_stop_blocked`: `peer-stop`, `ask-guard`) is not a
    turn end: the session goes on working, so its leases stay held and the deferred rings wait
    for the Stop that does end the turn."""
    if payload.get("agent_id") or payload.get("agent_type") or payload.get("_stop_blocked"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    from ..leases import HOLDER_STOPPED_KEY
    from ..ops.peer_wake_policy import ring_expired

    sessions.set_counter(root, session_id, HOLDER_STOPPED_KEY, time.time_ns() // 1_000_000)
    ring_expired(root, session_id)
    return ""


# ------------------------------------------------------------------ journal-pending

JOURNAL_SEEN_KEY = "journal_nag_seen"  # `:<day>` -> the pending fingerprint last nudged with


def journal_pending(root: Path, payload: dict[str, Any]) -> str:
    """Nudge each manager on UserPromptSubmit when the pending milestone fingerprint changes. Both
    clients accept additionalContext there; Codex rejects it on Stop, and blocking Stop would force
    a needless turn. A journal write clears covered milestones and leaves the next prompt quiet.
    """
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return ""
    if payload.get("agent_id") or payload.get("agent_type"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    from ..core import today
    from ..journal import is_writer
    from ..journal import pending as pending_mod

    if not is_writer(root):
        return ""
    day = today()
    seen = f"{JOURNAL_SEEN_KEY}:{day.isoformat()}"
    current = pending_mod.fingerprint(root, day)
    if not current or (sessions.get(root, session_id) or {}).get(seen) == current:
        return ""
    line = pending_mod.summary(root, day)
    if not line:
        return ""
    sessions.set_field(root, session_id, seen, current)
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": line}}
    )


# ------------------------------------------------------------------ hook-denial counting


def note_hook_denial(root: Path, payload: dict[str, Any], out: str) -> str:
    """Count this harness's own PreToolUse denials toward the thrash threshold.

    `PermissionDenied` only fires in auto mode. It never fires for a PreToolUse hook deny or a
    settings deny rule (hooks reference, read). So the denial streak in
    `thrash_guard` would never move on the denials this harness actually issues: 30 of them
    across four sessions in the audited week, all from `no-chaining` and `protect-generated`.
    So the count happens here, in the one place every hook's output passes through. At the
    threshold, the thrash line is appended to the deny reason itself, the text the model reads,
    and the streak resets. A successful PostToolUse resets it too.
    """
    if not out or '"permissionDecision": "deny"' not in out:
        return out
    if payload.get("agent_id") or payload.get("agent_type"):
        return out
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return out
    thrash = sessions.get_thrash(root, session_id)
    thrash["denials"] = int(thrash.get("denials") or 0) + 1
    if thrash["denials"] < THRASH_DENIALS:
        sessions.set_thrash(root, session_id, thrash)
        return out
    count = thrash["denials"]
    thrash["denials"] = 0
    sessions.set_thrash(root, session_id, thrash)
    data = json.loads(out)
    reason = str(data["hookSpecificOutput"].get("permissionDecisionReason") or "")
    data["hookSpecificOutput"]["permissionDecisionReason"] = (
        f"{reason} {_thrash_line(f'{count} consecutive hook denials')}"
    ).strip()
    return json.dumps(data)


# ------------------------------------------------------------------ thrash-guard

THRASH_DENIALS = 3  # consecutive PermissionDenied events
THRASH_ERRORS = 5  # consecutive PostToolUseFailure events
THRASH_REPEATS = 3  # the same Bash command, back to back
# The edit signal is about the same TEXT, not the same path. Counting edits per path
# fired twice in one session on progress: six different export and import edits to one
# package `__init__.py`, and six different edits adding one feature to one module. A loop
# re-edits what it just wrote, or puts back what it just took out.
THRASH_SAME_SPOT = 3  # an edit chain where each edit rewrites text the previous one wrote
THRASH_REVERTS = 2  # edits on one file that undo an earlier edit exactly
THRASH_HISTORY = 10  # rolling window length for both commands and edits
THRASH_EDIT_TEXT_CHARS = 2000  # how much of old/new text an edit record keeps for matching
# Repeated edits to a generated rubric or a scratch draft do not mean an edit loop. The
# first live firing of this hook was six ordinary changelog appends in ten edits; that
# changelog and the playbook have since been deleted.
# `.agent/scratch/plans/` joins them for the same reason, one step sideways: a
# plan-mode draft is BUILT by repeated edits to one file, so the signal fires on the tool
# working exactly as designed. Matched as a path fragment, not a suffix. The file inside is
# named per plan. Claude Code's per-session scratchpad (`/tmp/claude-<uid>/<project>/<session>/
# scratchpad/`) joins for the same reason: an artifact built there
# is one file edited per round of feedback, and the hook fired on every round.
THRASH_EDIT_EXEMPT = ("harness/dispatch-rubric.md",)
THRASH_EDIT_EXEMPT_DIRS = (agent_path(PurePosixPath(), "scratch", "plans").as_posix() + "/", "/scratchpad/")


def _thrash_edit_exempt(path: str) -> bool:
    if sys.platform == "win32":
        path = slashes(path)
    return path.endswith(THRASH_EDIT_EXEMPT) or any(d in path for d in THRASH_EDIT_EXEMPT_DIRS)


THRASH_OVERLAP_MIN_CHARS = 20  # below this, only identical text counts as the same spot


def _overlaps(written: str, replaced: str) -> bool:
    """Whether an edit's `old_string` is text an earlier edit wrote. Containment either way,
    so a widened or narrowed re-edit counts. A short fragment (a closing bracket, one word) is
    everywhere, so it only counts when the two are identical."""
    a, b = written.strip(), replaced.strip()
    if not a or not b:
        return False
    if min(len(a), len(b)) < THRASH_OVERLAP_MIN_CHARS:
        return a == b
    return a in b or b in a


def _edit_record(tool: str, path: str, tool_input: dict[str, Any], history: list[Any]) -> dict[str, Any]:
    """One edit in the window, with `depth`: how long the chain of edits re-editing each
    other's text is, ending at this one. A Write replaces the whole file, so a Write after an
    earlier edit of the same file always extends that chain."""
    if tool == "Write":
        old, new = "", str(tool_input.get("content") or "")
    else:
        old, new = str(tool_input.get("old_string") or ""), str(tool_input.get("new_string") or "")
    old, new = old[:THRASH_EDIT_TEXT_CHARS], new[:THRASH_EDIT_TEXT_CHARS]
    depth, revert = 1, False
    for prior in dict_list(history):
        if prior.get("path") != path:
            continue
        p_old, p_new = str(prior.get("old") or ""), str(prior.get("new") or "")
        if old and new and p_old == new and p_new == old:
            revert = True
        if tool == "Write" or _overlaps(p_new, old):
            depth = max(depth, (as_int(prior.get("depth")) or 1) + 1)
    return {"path": path, "old": old, "new": new, "depth": depth, "revert": revert}


def _thrash_line(what: str) -> str:
    return f"thrash signal: {what}. Stop, restate the goal in one sentence, then ask the owner or log a dead-end."


def thrash_guard(root: Path, payload: dict[str, Any]) -> str:
    """Session-local repetition detector.

    Wired to PermissionDenied, PostToolUseFailure, and PostToolUse (Bash|Edit|Write):
    consecutive denials, consecutive tool errors, the same Bash command run back to back, and
    the same text edited over and over or undone within a short window. Crossing a threshold injects one
    line of `additionalContext` and resets that counter. The model gets one nudge per episode,
    not one per call. State lives in the session record (`sessions.get_thrash`/`set_thrash`),
    so it survives across the many separate hook invocations one session makes.

    Verified against the current hooks reference (code.claude.com/docs/en/hooks.md) while
    building this: `PostToolUse` and `PostToolUseFailure` both honor
    `hookSpecificOutput.additionalContext`, matching what `session-start` and `dispatch-inbox`
    already do here. `PermissionDenied` does not. Its decision control table lists only
    `hookSpecificOutput.retry`, so the line built below is emitted in the same shape for
    consistency, but is not actually documented to reach the model on that event. More to the
    point, `PermissionDenied` "only fires in auto mode: it doesn't run when you manually deny
    a permission dialog, when a PreToolUse hook blocks a call, or when a deny rule matches".
    That covers every way this harness denies something (protect-generated, no-chaining, and
    the settings.json deny list). So this harness's own hook denials are counted in `run()` via
    `note_hook_denial`, which shares the `denials` streak below and appends the thrash line
    to the deny reason at the threshold. The `PermissionDenied` branch here covers auto mode.
    PostToolUse and PostToolUseFailure are where the other signals land.
    """
    if payload.get("agent_id") or payload.get("agent_type"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    event = str(payload.get("hook_event_name") or "")
    thrash = sessions.get_thrash(root, session_id)
    line = ""
    if event == "PermissionDenied":
        thrash["denials"] = int(thrash.get("denials") or 0) + 1
        if thrash["denials"] >= THRASH_DENIALS:
            line = _thrash_line(f"{thrash['denials']} consecutive permission denials")
            thrash["denials"] = 0
    elif event == "PostToolUseFailure":
        thrash["errors"] = int(thrash.get("errors") or 0) + 1
        if thrash["errors"] >= THRASH_ERRORS:
            line = _thrash_line(f"{thrash['errors']} consecutive tool errors")
            thrash["errors"] = 0
    elif event == "PostToolUse":
        thrash["denials"] = 0
        thrash["errors"] = 0
        tool = str(payload.get("tool_name") or "")
        tool_input = as_dict(payload.get("tool_input"))
        if tool == "Bash":
            cmd = str(tool_input.get("command") or "")
            history = (list(thrash.get("commands") or []) + [cmd])[-THRASH_HISTORY:]
            thrash["commands"] = history
            streak = 0
            for c in reversed(history):
                if c != cmd:
                    break
                streak += 1
            if cmd and streak >= THRASH_REPEATS:
                line = _thrash_line(f'the same Bash command {streak} times in a row: "{cmd[:80]}"')
                thrash["commands"] = []
        elif tool in ("Edit", "Write"):
            path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
            if path and not _thrash_edit_exempt(path):
                # Older records were bare path strings; `_edit_record` skips them.
                prior = list(thrash.get("edits") or [])[-(THRASH_HISTORY - 1) :]
                record = _edit_record(tool, path, tool_input, prior)
                history = prior + [record]
                thrash["edits"] = history
                reverts = sum(1 for e in dict_list(history) if e.get("path") == path and e.get("revert"))
                if record["depth"] >= THRASH_SAME_SPOT:
                    line = _thrash_line(
                        f"{path}: the same text edited {record['depth']} times in the last {len(history)} edits"
                    )
                    thrash["edits"] = []
                elif record["revert"] and reverts >= THRASH_REVERTS:
                    line = _thrash_line(f"{path}: {reverts} edits undid an earlier edit")
                    thrash["edits"] = []
    else:
        return ""
    sessions.set_thrash(root, session_id, thrash)
    if not line:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": line}})
