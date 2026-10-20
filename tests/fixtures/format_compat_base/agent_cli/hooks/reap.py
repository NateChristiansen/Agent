# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any

HOOK_GRACE_SECONDS = 2.0
"Shorter than the reaper's default: a SessionEnd hook holds up the session's exit, and anything\nthat survives SIGTERM for two seconds is SIGKILLed, not waited on."


def session_reap(root: Path, payload: dict[str, Any]) -> str:
    from .. import procs

    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    targets = procs.select(procs.snapshot(), session=session_id, escaped_only=True)
    if targets:
        procs.reap(root, targets, grace=HOOK_GRACE_SECONDS)
    return ""
