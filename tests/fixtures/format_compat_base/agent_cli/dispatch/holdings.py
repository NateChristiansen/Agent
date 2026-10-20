# ruff: noqa
from __future__ import annotations
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from .. import core
from .. import leases as leases_mod
from .. import repos as repos_mod
from ..core import AgentError
from ..records.narrow import as_dict, as_dict_or_none, as_int, as_list_or_none
from . import lane, telemetry_events
from .allocation import owned_pids, refuse_cutover, worktree_area, worktree_key

SLOT_SCHEMA = 1
DEFAULT_CAP = 3
HELD_AREAS = ("repo:", "worktree:", "repo-slot:", "verify:")
REVERIFY = "reverify:"
Taken = list[tuple[str, str]]


def slot_area(short: str, k: int) -> str:
    return f"repo-slot:{short}/{k}"


def cap_of(rp: repos_mod.Repo) -> int:
    if "max_concurrent" not in rp.dispatch:
        return DEFAULT_CAP
    return as_int(rp.dispatch["max_concurrent"], 1) or 1


def cap(root: Path, d: dict[str, Any]) -> int:
    return cap_of(repos_mod.load_registry(root).require(d["repo"]))


def stored_slot(d: dict[str, Any], limit: int | None = None) -> int | None:
    if "slot_schema" not in d:
        return None
    k = d.get("slot")
    if d["slot_schema"] != SLOT_SCHEMA or type(k) is not int or k < 0 or (limit is not None and k >= limit):
        raise AgentError(f"dispatch {d['id']} has slot_schema but no valid slot")
    return k


def _take(root: Path, area: str, sid: str, note: str | None, **marks: Any) -> bool:
    held = leases_mod.get(root, area)
    leases_mod.take_marked(root, area, session_id=sid, pid=os.getpid(), note=note, **marks)
    return held is None or held.get("session_id") != sid


def _take_worktree(root: Path, d: dict[str, Any], note: str | None, taken: Taken) -> None:
    key, sid = (d["worktree_key"], f"dispatch:{d['id']}")
    area = worktree_area(d["short"], key)
    try:
        if _take(root, area, sid, note, worktree_key=key):
            taken.append((area, sid))
    except AgentError as exc:
        held = leases_mod.get(root, area)
        if held is None:
            raise
        raise AgentError(
            f"worktree {key} is reserved by {held.get('session_id')}; it is still allocating or running there. Wait for it, or start a dispatch on another worktree"
        ) from exc


def pick(
    root: Path,
    d: dict[str, Any],
    sid: str,
    note: str | None,
    limit: int,
    taken: Taken,
    area_of: Callable[[str, int], str] = slot_area,
    what: str = "repo slot",
    **marks: Any,
) -> int:
    refusals: list[str] = []
    for k in range(limit):
        area = area_of(d["short"], k)
        try:
            if _take(root, area, sid, note, **marks):
                taken.append((area, sid))
            return k
        except AgentError as exc:
            refusals.append(str(exc))
    if limit == 1:
        raise AgentError(refusals[0])
    raise AgentError(f"every {what} for {d['short']} is held (limit {limit}): " + " ".join(refusals))


def rollback(root: Path, taken: Taken) -> None:
    for area, sid in reversed(taken):
        try:
            leases_mod.release(root, area, sid)
        except Exception:
            pass


def acquire(root: Path, d: dict[str, Any], *, note: str | None = None, limit: int | None = None) -> Taken:
    d["worktree_key"] = d.get("worktree_key") or worktree_key(d["worktree"])
    d["lease_protocol"] = leases_mod.LEASE_PROTOCOL
    sid, taken = (f"dispatch:{d['id']}", Taken())
    try:
        _take_worktree(root, d, note, taken)
        k = stored_slot(d, limit)
        if k is not None:
            if _take(root, slot_area(d["short"], k), sid, note):
                taken.append((slot_area(d["short"], k), sid))
        elif limit is not None:
            d["slot"], d["slot_schema"] = (pick(root, d, sid, note, limit, taken), SLOT_SCHEMA)
    except BaseException:
        rollback(root, taken)
        raise
    return taken


def hold(root: Path, d: dict[str, Any], *, note: str | None = None) -> None:
    acquire(root, d, note=note)


def _held_message(d: dict[str, Any]) -> str:
    held = as_dict(d.get("holdings_held"))
    return f"dispatch {d['id']} keeps its worktree lease and repo slot: {held.get('reason')} (pids {held.get('pids') or []}); collect it again once its processes stop"


def resume(root: Path, d: dict[str, Any], *, note: str) -> Taken:
    refuse_cutover(root, d)
    if d.get("holdings_held"):
        raise AgentError(_held_message(d))
    limit = cap(root, d)
    if stored_slot(d, limit) is None:
        released = slot_released(d)
        pids, idle = _ruled_out(root, d, reaped=released and cleanup_confirmed(d))
        if not idle:
            why = f"its processes {pids} still run" if pids else "the process inventory is not trustworthy"
            what = "gave its repo slot back" if released else "predates repo slots"
            raise AgentError(
                f"dispatch {d['id']} {what} and {why}; resume it once that clears, nothing was changed"
            )
    return acquire(root, d, note=note, limit=limit)


def cleanup_confirmed(d: dict[str, Any]) -> bool:
    return (
        d.get("worker_cleanup_confirmed") is True
        and (not d.get("worker_cleanup_pending"))
        and (not d.get("claude_pid"))
        and (not d.get("cancel_unverified_pids"))
        and (not d.get("cancel_inventory_error"))
    )


def _ruled_out(root: Path, d: dict[str, Any], *, reaped: bool | None = None) -> tuple[list[int], bool]:
    pids, trusted = owned_pids(root, d, reaped=cleanup_confirmed(d) if reaped is None else reaped)
    return (pids, trusted and (not pids))


def _doubt(d: dict[str, Any]) -> bool:
    if d.get("cancel_unverified_pids") or d.get("cancel_inventory_error"):
        return True
    sup = d.get("supervisor_pid")
    return bool(d.get("attempt_started")) and sup not in (None, os.getpid()) and (not core.is_alive(sup))


def held_areas(root: Path, sid: str, kinds: tuple[str, ...]) -> list[str]:
    areas = [
        str(lease["area"])
        for lease in leases_mod.list_all(root)
        if lease.get("session_id") == sid and str(lease.get("area") or "").startswith(kinds)
    ]
    return sorted(areas, key=lambda area: area.startswith("worktree:"))


def _mark_held(root: Path, d: dict[str, Any], pids: list[int]) -> None:
    from .state import save
    from .verification_gate import record_guard

    reason = "an owned process may still run" if pids else "the process inventory was not trustworthy"
    with record_guard(root, d["id"]) as current:
        current["holdings_held"] = {"pids": pids, "reason": reason}
        save(root, current)
    d["holdings_held"], d["record_revision"] = (current["holdings_held"], current["record_revision"])


def _free(root: Path, sid: str, kinds: tuple[str, ...] = ("repo-slot:", "worktree:")) -> list[str]:
    return [area for area in held_areas(root, sid, kinds) if leases_mod.release(root, area, sid)]


def release(root: Path, d: dict[str, Any]) -> None:
    try:
        if _doubt(d):
            pids, idle = _ruled_out(root, d)
            if not idle:
                _mark_held(root, d, pids)
                return
        lane.tidy(root, d)
        _free(root, f"dispatch:{d['id']}", HELD_AREAS)
    except Exception:
        pass


def abandon(root: Path, d: dict[str, Any]) -> None:
    if d.get("holdings_held"):
        raise AgentError(_held_message(d))
    _free(root, f"dispatch:{d['id']}")


def run_blocks(root: Path, lease: dict[str, Any]) -> bool:
    from .state import load
    from .verification_evidence import binding_of

    try:
        _, dispatch_id, run_id = str(lease["session_id"]).split(":", 2)
        try:
            rows = as_list_or_none(load(root, dispatch_id).get("reverify_runs") or [])
        except core.NotFound:
            rows = []
        if rows is None:
            return True
        for item in rows:
            row = as_dict_or_none(item)
            binding = as_dict_or_none(row.get("binding") or {}) if row is not None else None
            if row is None or binding is None:
                return True
            if run_id in (row.get("run_id"), binding.get("run_id")):
                busy = bool(binding) and lane.run_busy(root, binding_of(binding))
                return bool(row.get("cleanup_pending")) or busy
        checkout = lease.get("checkout")
        return bool(checkout) and Path(str(checkout)).exists()
    except Exception:
        return True


def reverify_take(root: Path, d: dict[str, Any], run_id: str, checkout: Path) -> Taken:
    from .verification_gate import requires_lease

    refuse_cutover(root, d)
    if requires_lease(root, d) or d.get("holdings_held"):
        raise AgentError(f"dispatch {d['id']} has a live worker or unconfirmed cleanup; reverify refused")
    target = {**d, "worktree_key": d.get("worktree_key") or worktree_key(d["worktree"])}
    note, taken = (f"reverify {d['id']} {run_id}", Taken())
    try:
        _take_worktree(root, target, note, taken)
        sid = f"{REVERIFY}{d['id']}:{run_id}"
        taken += lane.take_now(root, target, sid, note, marks={"checkout": str(checkout)})[1]
    except BaseException:
        rollback(root, taken)
        raise
    return taken


def reverify_release(root: Path, d: dict[str, Any], run_id: str) -> None:
    try:
        _free(root, f"{REVERIFY}{d['id']}:{run_id}", ("verify:",))
        _free(root, f"dispatch:{d['id']}")
    except Exception:
        pass


def reverify_held(root: Path, dispatch_id: str) -> dict[str, list[tuple[str, str]]]:
    lane_sid, note_of = (f"{REVERIFY}{dispatch_id}:", f"reverify {dispatch_id} ")
    held: dict[str, list[tuple[str, str]]] = {}
    for lease in leases_mod.list_all(root):
        sid, area, note = (str(lease.get(key) or "") for key in ("session_id", "area", "note"))
        if sid.startswith(lane_sid) and area.startswith("verify:"):
            held.setdefault(sid.removeprefix(lane_sid), []).insert(0, (area, sid))
        elif sid == f"dispatch:{dispatch_id}" and area.startswith("worktree:") and note.startswith(note_of):
            held.setdefault(note.removeprefix(note_of), []).append((area, sid))
    return held


def reverify_free(root: Path, dispatch_id: str, run_id: str) -> bool:
    for area, sid in reverify_held(root, dispatch_id).get(run_id, []):
        leases_mod.release(root, area, sid)
    return run_id not in reverify_held(root, dispatch_id)


def reconcile(root: Path, *, limit: int) -> list[str]:
    from .state import load_all, save
    from .verification_gate import record_guard

    released: list[str] = []
    try:
        for d in [d for d in load_all(root) if d.get("holdings_held")][:limit]:
            if not _ruled_out(root, d)[1]:
                continue
            with record_guard(root, d["id"]) as current:
                if current.get("holdings_held") != d["holdings_held"]:
                    continue
                current["holdings_released"] = {**current.pop("holdings_held"), "reason": "no owned process"}
                save(root, current)
            released += _free(root, f"dispatch:{d['id']}")
        for lease in leases_mod.list_all(root)[:limit]:
            sid, pid, area = (
                str(lease.get("session_id") or ""),
                lease.get("pid"),
                str(lease.get("area") or ""),
            )
            if not area.startswith("verify:") or pid is None or core.is_alive(pid):
                continue
            if sid.startswith((REVERIFY, "dispatch:")) and lane.reconcile_one(root, lease):
                released.append(area)
            elif (
                sid.startswith(lane.RECORD)
                and _record_settled(root, sid)
                and leases_mod.release(root, area, sid)
            ):
                released.append(area)
    except Exception:
        pass
    return released


def record_unsettled(root: Path, sid: str) -> bool:
    from .. import verify_record, verify_run

    return verify_run.unsettled(verify_record.store(root) / "runs" / sid.removeprefix(lane.RECORD))


def _record_settled(root: Path, sid: str) -> bool:
    from .. import verify_record, verify_run

    run_dir = verify_record.store(root) / "runs" / sid.removeprefix(lane.RECORD)
    return verify_run.settle(verify_record.main_of(root), run_dir)


def preview(root: Path, rp: repos_mod.Repo) -> dict[str, Any]:
    limit, held = (cap_of(rp), list[dict[str, Any]]())
    for k in range(limit):
        lease = leases_mod.get(root, slot_area(rp.short, k))
        if lease is not None:
            held.append(
                {"slot": k, "session_id": lease.get("session_id"), "status": leases_mod.status(lease, root)}
            )
    busy = {row["slot"] for row in held if row["status"] in leases_mod.BLOCKING_STATUSES}
    retired = leases_mod.get(root, f"repo:{rp.short}")
    return {
        "repo_slots": {"cap": limit, "free": [k for k in range(limit) if k not in busy], "held": held},
        "repo_lease": {**retired, "status": leases_mod.status(retired, root)} if retired else None,
    }


def preview_lines(p: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if "repo_slots" in p:
        slots = p["repo_slots"]
        held = ", ".join((f"{h['slot']} by {h['session_id']} ({h['status']})" for h in slots["held"]))
        lines.append(
            f"  repo slots: {len(slots['free'])} of {slots['cap']} free" + (f"; {held}" if held else "")
        )
    lease = p.get("repo_lease")
    if lease:
        refuses = "; it refuses the launch" if lease.get("status") in leases_mod.BLOCKING_STATUSES else ""
        lines.append(
            f"  retired repo lease: held by {lease.get('session_id')} ({lease.get('status')}){refuses}"
        )
    elif "repo_lease" in p:
        lines.append("  retired repo lease: free")
    return lines


def refuse_busy(root: Path, short: str, worktree: Path) -> None:
    for area in (worktree_area(short, worktree), f"repo:{short}"):
        lease = leases_mod.get(root, area)
        if lease is not None and leases_mod.status(lease, root) in leases_mod.BLOCKING_STATUSES:
            raise AgentError(leases_mod.refusal(root, area, lease))


def cleanup_take(root: Path, short: str, worktree: Path, sid: str) -> Taken:
    area = worktree_area(short, worktree)
    leases_mod.take_marked(
        root, area, session_id=sid, pid=os.getpid(), note="cleanup", worktree_key=worktree_key(worktree)
    )
    return [(area, sid)]


def release_slot(root: Path, dispatch_id: str, attempt: int) -> bool:
    from .state import save
    from .verification_gate import record_guard

    with record_guard(root, dispatch_id) as d:
        if (
            d.get("attempt") != attempt
            or d.get("worker_cleanup_pending")
            or (not d.get("worker_cleanup_confirmed"))
        ):
            return False
        d["slot_released"] = {"attempt": attempt}
        save(root, d)
        freed = _free(root, f"dispatch:{dispatch_id}", ("repo-slot:",))
        d.pop("slot", None)
        d.pop("slot_schema", None)
        d["slot_released"] = {"attempt": attempt, "areas": freed}
        save(root, d)
    telemetry_events.transition(root, d, "awaiting-verification")
    return True


def slot_released(d: dict[str, Any]) -> bool:
    return as_dict(d.get("slot_released")).get("attempt") == d.get("attempt") and bool(
        d.get("worker_cleanup_confirmed")
    )


def pending_artifacts(root: Path, short: str) -> list[str]:
    from .state import TERMINAL_STATES, load_all

    return [
        str(d["id"])
        for d in load_all(root)
        if d.get("short") == short
        and d.get("state") not in TERMINAL_STATES
        and (as_dict(d.get("slot_released")).get("attempt") == d.get("attempt"))
        and core.is_alive(d.get("supervisor_pid"))
    ]


def pending_limit(rp: repos_mod.Repo) -> int:
    from .lane import lanes_of

    return as_int(rp.dispatch.get("max_pending_verify"), 0) or max(2, 2 * lanes_of(rp))


def refuse_backpressure(root: Path, rp: repos_mod.Repo) -> None:
    waiting = pending_artifacts(root, rp.short)
    limit = pending_limit(rp)
    if len(waiting) >= limit:
        raise AgentError(
            f"{len(waiting)} finished dispatches in {rp.short} await verification (limit {limit}: {', '.join(waiting)}); launch once one completes, or raise dispatch.max_pending_verify"
        )
