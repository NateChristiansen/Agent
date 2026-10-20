# ruff: noqa
from __future__ import annotations
import time
from pathlib import Path
from typing import Any
from .. import features, repos
from ..core import AgentError, is_alive, iso, now
from . import notice_producers
from . import verification_gate as gate
from .allocation import allocating, reconcile_allocations, refuse_allocating, release_held
from .driver import await_worker_stop, kill_group
from .evaluate import evaluate
from .notice_producers import reconcile
from .state import load, load_all, pending_outcome_of, save, summarize
from .terminal import finalize


def cancel(root: Path, *, dispatch_id: str) -> dict[str, Any]:
    with gate.record_guard(root, dispatch_id) as d:
        refuse_allocating(d, "cancel")
        if d.get("terminal_committed") or d["state"] not in ("queued", "running", "cancelled"):
            raise AgentError(f"dispatch {dispatch_id} is {d['state']}; nothing to cancel")
        was_running = d["state"] == "running"
        d["state"] = "cancelled"
        d["error"] = "cancelled by the manager"
        d["verification_cleanup_pending"] = True
        d["chat_pending"] = False
        if not d.get("cancel_requested"):
            d["cancel_requested"] = iso(now())
            d["thread"].append({"ts": iso(now()), "from": "manager", "text": "cancelled"})
        notice_producers.supersede(d, "cancel")
        save(root, d)
    if was_running and (not await_worker_stop(root, dispatch_id)) and d.get("claude_pid"):
        unknown: list[int] = []
        inventory_error = ""
        try:
            unknown = kill_group(int(d["claude_pid"]), root=root, dispatch_id=dispatch_id)
        except OSError as exc:
            inventory_error = f"{type(exc).__name__}: {exc}"
        if unknown or inventory_error:
            with gate.record_guard(root, dispatch_id) as current:
                current["cancel_unverified_pids"] = unknown
                current["cancel_inventory_error"] = inventory_error
                save(root, current)
    gate.settle(root, dispatch_id, cancel=True)
    feat = features.resolve(root, str(d["feature"]))
    current = load(root, dispatch_id)
    outcome = evaluate(root, current, repos.load_registry(root).find(str(current.get("repo") or "")))
    return finalize(root, current, feat, outcome)


def collect(root: Path, *, dispatch_id: str | None = None, wait: float = 0) -> list[dict[str, Any]]:
    targets = (
        [load(root, dispatch_id)]
        if dispatch_id
        else [
            d
            for d in load_all(root)
            if d["state"] in ("queued", "running")
            or d.get("verification_cleanup_pending")
            or (d["state"] == "cancelled" and (not d.get("terminal_committed")))
        ]
    )
    deadline = time.monotonic() + float(wait or 0)
    registry = repos.load_registry(root)
    out: list[dict[str, Any]] = []
    for d in targets:
        while (
            d["state"] in ("queued", "running")
            and (is_alive(d.get("supervisor_pid")) or allocating(d))
            and (time.monotonic() < deadline)
        ):
            time.sleep(0.2)
            d = load(root, d["id"])
        d = release_held(root, d)
        if (
            not d.get("terminal_committed")
            and (not allocating(d))
            and (
                d["state"] == "cancelled"
                or d.get("pending_outcome")
                or (d["state"] in ("queued", "running") and (not is_alive(d.get("supervisor_pid"))))
            )
        ):
            gate.settle(root, d["id"], cancel=d["state"] not in ("queued", "running"), timeout=0)
            d = load(root, d["id"])
            outcome = pending_outcome_of(d) or evaluate(root, d, registry.find(str(d.get("repo") or "")))
            outcome["auto_resume"] = None
            if outcome["state"] in ("queued", "running"):
                outcome["state"] = "failed"
            feat = features.resolve(root, str(d["feature"]))
            d = finalize(root, d, feat, outcome)
        out.append(summarize(d))
    reconcile_allocations(root)
    reconcile(root, [dispatch_id] if dispatch_id else None)
    return out
