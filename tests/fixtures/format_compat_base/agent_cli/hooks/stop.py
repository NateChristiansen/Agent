# ruff: noqa
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


def lease_yield(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("agent_id") or payload.get("agent_type") or payload.get("_stop_blocked"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    from ..leases import HOLDER_STOPPED_KEY
    from ..ops.peer_wake_policy import ring_expired

    sessions.set_counter(root, session_id, HOLDER_STOPPED_KEY, time.time_ns() // 1000000)
    ring_expired(root, session_id)
    return ""


JOURNAL_SEEN_KEY = "journal_nag_seen"


def journal_pending(root: Path, payload: dict[str, Any]) -> str:
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


def note_hook_denial(root: Path, payload: dict[str, Any], out: str) -> str:
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
        f"{reason} {_thrash_line(f'{count} consecutive hook denials')}".strip()
    )
    return json.dumps(data)


THRASH_DENIALS = 3
THRASH_ERRORS = 5
THRASH_REPEATS = 3
THRASH_SAME_SPOT = 3
THRASH_REVERTS = 2
THRASH_HISTORY = 10
THRASH_EDIT_TEXT_CHARS = 2000
THRASH_EDIT_EXEMPT = ("harness/dispatch-rubric.md",)
THRASH_EDIT_EXEMPT_DIRS = (agent_path(PurePosixPath(), "scratch", "plans").as_posix() + "/", "/scratchpad/")


def _thrash_edit_exempt(path: str) -> bool:
    if sys.platform == "win32":
        path = slashes(path)
    return path.endswith(THRASH_EDIT_EXEMPT) or any((d in path for d in THRASH_EDIT_EXEMPT_DIRS))


THRASH_OVERLAP_MIN_CHARS = 20


def _overlaps(written: str, replaced: str) -> bool:
    a, b = (written.strip(), replaced.strip())
    if not a or not b:
        return False
    if min(len(a), len(b)) < THRASH_OVERLAP_MIN_CHARS:
        return a == b
    return a in b or b in a


def _edit_record(tool: str, path: str, tool_input: dict[str, Any], history: list[Any]) -> dict[str, Any]:
    if tool == "Write":
        old, new = ("", str(tool_input.get("content") or ""))
    else:
        old, new = (str(tool_input.get("old_string") or ""), str(tool_input.get("new_string") or ""))
    old, new = (old[:THRASH_EDIT_TEXT_CHARS], new[:THRASH_EDIT_TEXT_CHARS])
    depth, revert = (1, False)
    for prior in dict_list(history):
        if prior.get("path") != path:
            continue
        p_old, p_new = (str(prior.get("old") or ""), str(prior.get("new") or ""))
        if old and new and (p_old == new) and (p_new == old):
            revert = True
        if tool == "Write" or _overlaps(p_new, old):
            depth = max(depth, (as_int(prior.get("depth")) or 1) + 1)
    return {"path": path, "old": old, "new": new, "depth": depth, "revert": revert}


def _thrash_line(what: str) -> str:
    return f"thrash signal: {what}. Stop, restate the goal in one sentence, then ask the owner or log a dead-end."


def thrash_guard(root: Path, payload: dict[str, Any]) -> str:
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
            if path and (not _thrash_edit_exempt(path)):
                prior = list(thrash.get("edits") or [])[-(THRASH_HISTORY - 1) :]
                record = _edit_record(tool, path, tool_input, prior)
                history = prior + [record]
                thrash["edits"] = history
                reverts = sum((1 for e in dict_list(history) if e.get("path") == path and e.get("revert")))
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
