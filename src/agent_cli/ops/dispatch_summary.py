"""Return compact dispatch outcomes and the last sign of worker activity to bound manager context.
verbose or show provides the full record. Stream activity describes silence without stopping work:
legitimate reasoning may produce no output.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..dispatch.state import file
from ..records.dispatch import result_of
from ..records.narrow import as_dict_or_none

KEYS = (
    "id",
    "state",
    "attempt",
    "provider",
    "model",
    "row",
    "branch",
    "worktree",
    "writes",
    "budget_usd",
    "cost_usd",
    "handoff",
    "chat_pending",
)
#: Minutes without a stream write after which a running worker is reported as silent.
SILENT_MINUTES = 10


def compact(d: dict[str, Any]) -> dict[str, Any]:
    """The fields a manager acts on, from a full dispatch record."""
    out = {k: d.get(k) for k in KEYS if k in d}
    res = result_of(d)
    if res.get("status") or res.get("summary"):
        out["result"] = {"status": res.get("status"), "summary": str(res.get("summary") or "")[:500]}
    if d.get("error"):
        out["error"] = str(d["error"])[:500]
    verification = as_dict_or_none(d.get("verification_result"))
    if verification is not None:
        out["verification"] = verification.get("status")
    out["detail"] = "full record: dispatch action=show, or verbose=True"
    return out


def activity(root: Path, d: dict[str, Any], *, now: float | None = None) -> str:
    """`last activity: … ; waiting on: …` for a dispatch, or "" when nothing applies."""
    state = str(d.get("state") or "")
    clock = time.time() if now is None else now
    stamps = [
        p.stat().st_mtime for p in (file(root, d["id"], ".out"), file(root, d["id"], ".err")) if p.exists()
    ]
    last = max(stamps) if stamps else None
    age = None if last is None else max(0.0, (clock - last) / 60)
    seen = "no worker output yet" if age is None else f"{age:.0f} min ago"
    if state == "running" and d.get("open_question"):
        waiting = "an answer: the worker holds its question open (resume or say)"
    elif state == "running":
        silent = age is not None and age >= SILENT_MINUTES
        waiting = (
            f"the worker, silent for {age:.0f} min (not stopped; check `dispatch spans`, then `say` or "
            "`cancel`)"
            if silent
            else "the worker"
        )
    elif state == "queued":
        waiting = "a start: the verification lane or a slot"
    elif state == "question":
        waiting = "an answer (resume)"
    elif state == "done" and not d.get("actioned_at"):
        waiting = "landing or ack"
    else:
        return ""
    return f"  last activity: {seen}; waiting on: {waiting}"
