# ruff: noqa
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any
from .. import core


def worker_stop(root: Path, payload: dict[str, Any]) -> str:
    from .. import dispatch as dispatch_mod

    dispatch_mod.worker_stop(root, payload)
    return ""


def precompact(root: Path, payload: dict[str, Any]) -> str:
    from .. import dispatch as dispatch_mod

    dispatch_id = os.environ.get("AGENT_DISPATCH_ID") or ""
    if not dispatch_id:
        return ""
    try:
        d = dispatch_mod.load(root, dispatch_id)
    except core.AgentError:
        return ""
    fields = ", ".join(dispatch_mod.RETURN_SCHEMA["required"])
    ctx = f"[worker reorientation — your context was just compacted. dispatch {dispatch_id}. These two were in your first message only, so they are re-stated here.]\nnonce: {d['nonce']}\nEcho this nonce verbatim in the `nonce` field of your return. A mismatch fails the dispatch outright; it is not retried.\nYour final message must be the JSON return, with every field: {fields}. `status` is one of {', '.join(dispatch_mod.RETURN_STATUSES)}.\nYou are in {d['worktree']} on branch {d['branch']}. Commit there; never push."
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": ctx}})
