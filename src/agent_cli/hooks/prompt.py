"""Deliver a manager's inbox and record prompt activity as one ordered hook.

An empty queued wake, peer or dispatch, is blocked by the inbox. The client emits no Stop for that blocked
prompt, so a separate activity hook could leave the tab showing working indefinitely.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import sessions
from ..records.narrow import as_dict
from .activity import activity, codex_activity
from .session import dispatch_inbox, session_start


def prompt(root: Path, payload: dict[str, Any], activity_hook: Callable[[Path, dict[str, Any]], str]) -> str:
    inbox = dispatch_inbox(root, payload)
    if inbox:
        try:
            if json.loads(inbox).get("decision") == "block":
                # No turn starts and no Stop follows. Keep the preceding state: normally idle,
                # but also preserve a pending owner question or a real working turn.
                return inbox
        except json.JSONDecodeError, AttributeError:
            pass
    try:
        # The inbox let this prompt through, so a wake among them starts a turn.
        activity_hook(root, {**payload, "_wake_accepted": True})
    except Exception as exc:  # noqa: BLE001 — inbox delivery survives a courtesy activity failure
        sys.stderr.write(f"note: prompt activity failed ({exc})\n")
    return inbox


def manager_prompt(root: Path, payload: dict[str, Any]) -> str:
    return prompt(root, payload, activity)


def codex_prompt(root: Path, payload: dict[str, Any]) -> str:
    # Codex 0.155/0.156 omits SessionStart on `codex resume`. The old record still has the
    # previous process and tab, so ordinary relabels target a closed tab even after a prompt.
    # The first prompt in the new process is the earliest reliable hook boundary.
    sid = str(payload.get("session_id") or "")
    record = (sessions.get(root, sid) or {}) if sid else {}
    pid = sessions.resolve_pid()
    resumed = bool(record and record.get("pid") and pid and record["pid"] != pid)
    if resumed:
        old_display = str(record.get("tab_display_name") or "")
        for field in ("tab_display_name", "tab_name", "tab_label"):
            sessions.set_field(root, sid, field, None)
        payload = {**payload, "_resume_claim_untitled": True, "_resume_old_display": old_display}
    result = prompt(root, payload, codex_activity)
    if not resumed:
        return result
    if result and json.loads(result).get("decision") == "block":
        return result
    startup = ""
    try:
        raw = session_start(root, {**payload, "source": "resume"})
        startup = str(json.loads(raw)["hookSpecificOutput"]["additionalContext"])
    except KeyError, TypeError, ValueError:
        pass
    if not startup:
        return result
    if result:
        data = json.loads(result)
        output = as_dict(data.get("hookSpecificOutput"))
        context = str(output.get("additionalContext") or "")
        output["additionalContext"] = startup + ("\n\n" + context if context else "")
        return json.dumps(data)
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": startup}}
    )
