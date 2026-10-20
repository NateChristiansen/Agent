"""Hooks that run inside a dispatched worker, not the manager: `worker-stop` (Stop/SessionEnd)
and `precompact` (PreCompact). Both read `AGENT_DISPATCH_ID`/the dispatch record rather than a
session-to-feature mapping, since a worker has no feature of its own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import core


def worker_stop(root: Path, payload: dict[str, Any]) -> str:
    """Stop/SessionEnd inside a dispatched worker.

    Records its transcript path and last message on the dispatch. Routes the capture into the
    feature's .agent/raw/<slug>/ with dispatch_id."""
    from .. import dispatch as dispatch_mod

    dispatch_mod.worker_stop(root, payload)
    return ""


def precompact(root: Path, payload: dict[str, Any]) -> str:
    """PreCompact inside a dispatched worker: re-inject the nonce and the return shape.

    Both live in the worker's FIRST message only (`dispatch.compose_prompt`). A nonce mismatch
    is a terminal `failed` in `evaluate` with no retry. So a worker long enough to compact
    could lose the one string its entire return is validated on, with no way back to it (audit section E). The dispatch id arrives in `AGENT_DISPATCH_ID`, which is
    environment rather than context, so it survives a compaction untouched.
    """
    from .. import dispatch as dispatch_mod

    dispatch_id = os.environ.get("AGENT_DISPATCH_ID") or ""
    if not dispatch_id:
        return ""
    try:
        d = dispatch_mod.load(root, dispatch_id)
    except core.AgentError:
        return ""
    fields = ", ".join(dispatch_mod.RETURN_SCHEMA["required"])
    ctx = (
        f"[worker reorientation — your context was just compacted. dispatch {dispatch_id}. "
        "These two were in your first message only, so they are re-stated here.]\n"
        f"nonce: {d['nonce']}\n"
        "Echo this nonce verbatim in the `nonce` field of your return. A mismatch fails the "
        "dispatch outright; it is not retried.\n"
        "Your final message must be the JSON return, with every field: "
        f"{fields}. `status` is one of {', '.join(dispatch_mod.RETURN_STATUSES)}.\n"
        f"You are in {d['worktree']} on branch {d['branch']}. Commit there; never push."
    )
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": ctx}})
