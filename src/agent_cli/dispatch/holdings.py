"""Manage per-worktree leases and numbered repository slots.

A dispatch takes its worktree lease, then the first free slot below `dispatch.max_concurrent`
(default 3), as non-waiting tries under `dispatch-alloc`. Persist the slot with `slot_schema: 1`;
later re-takes and releases use that slot. A refused acquisition releases only holdings newly taken
by that call. A live legacy `repo:<short>` lease refuses launch (`allocation.refuse_cutover`).
Reverify uses a verification lane instead.

Keep holdings while cleanup is unconfirmed or a trustworthy inventory cannot rule out owned
processes (`verification_gate.lease_blocks`). Mark `holdings_held` until reconciliation proves
release safe.
"""

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
    """Return stored k, or None for a legacy record without slot_schema. Refuse an invalid or missing
    slot when the schema claims one, including one outside the supplied limit.
    """
    if "slot_schema" not in d:
        return None
    k = d.get("slot")
    if d["slot_schema"] != SLOT_SCHEMA or type(k) is not int or k < 0 or (limit is not None and k >= limit):
        raise AgentError(f"dispatch {d['id']} has slot_schema but no valid slot")
    return k


def _take(root: Path, area: str, sid: str, note: str | None, **marks: Any) -> bool:
    """One non-waiting take; True when the lease was not already this holder's."""
    held = leases_mod.get(root, area)
    leases_mod.take_marked(root, area, session_id=sid, pid=os.getpid(), note=note, **marks)
    return held is None or held.get("session_id") != sid


def _take_worktree(root: Path, d: dict[str, Any], note: str | None, taken: Taken) -> None:
    key, sid = d["worktree_key"], f"dispatch:{d['id']}"
    area = worktree_area(d["short"], key)
    try:
        if _take(root, area, sid, note, worktree_key=key):
            taken.append((area, sid))
    except AgentError as exc:
        held = leases_mod.get(root, area)
        if held is None:
            raise
        raise AgentError(
            f"worktree {key} is reserved by {held.get('session_id')}; it is still allocating or "
            "running there. Wait for it, or start a dispatch on another worktree"
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
    """The capped-lease helper: the first of `limit` numbered leases this holder can take now."""
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
    """Release what one call newly took, newest first, holder-checked and best effort."""
    for area, sid in reversed(taken):
        try:
            leases_mod.release(root, area, sid)
        except Exception:  # noqa: BLE001, S110 — a kept lease is freed by the reconciler or its holder
            pass


def acquire(root: Path, d: dict[str, Any], *, note: str | None = None, limit: int | None = None) -> Taken:
    """Take the worktree lease, then the stored slot or first free slot under limit. Persist newly
    allocated slots with slot_schema and return only holdings newly taken. Without limit, legacy
    records touch only the worktree lease.
    """
    d["worktree_key"] = d.get("worktree_key") or worktree_key(d["worktree"])
    d["lease_protocol"] = leases_mod.LEASE_PROTOCOL
    sid, taken = f"dispatch:{d['id']}", Taken()
    try:
        _take_worktree(root, d, note, taken)
        k = stored_slot(d, limit)
        if k is not None:
            if _take(root, slot_area(d["short"], k), sid, note):
                taken.append((slot_area(d["short"], k), sid))
        elif limit is not None:
            d["slot"], d["slot_schema"] = pick(root, d, sid, note, limit, taken), SLOT_SCHEMA
    except BaseException:
        rollback(root, taken)
        raise
    return taken


def hold(root: Path, d: dict[str, Any], *, note: str | None = None) -> None:
    """Touch `d`'s worktree lease and stored slot in this process: the supervisor's re-take and the
    re-takes before verification and acceptance. A refusal is the caller's to act on."""
    acquire(root, d, note=note)


def _held_message(d: dict[str, Any]) -> str:
    held = as_dict(d.get("holdings_held"))
    return (
        f"dispatch {d['id']} keeps its worktree lease and repo slot: {held.get('reason')} "
        f"(pids {held.get('pids') or []}); collect it again once its processes stop"
    )


def resume(root: Path, d: dict[str, Any], *, note: str) -> Taken:
    """Retake the exact worktree and slot under dispatch-alloc and the record guard. A legacy record
    can take a free slot only after trustworthy inventory rules out owned processes. Refusal
    preserves record and holdings.
    """
    refuse_cutover(root, d)
    if d.get("holdings_held"):
        raise AgentError(_held_message(d))
    limit = cap(root, d)
    if stored_slot(d, limit) is None:
        # A released slot proves cleanup under the slot protocol. Legacy records lack that
        # confirmation, so inventory must rule out owned processes.
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
    """The record says this attempt's worker tree is gone: the supervisor or a cancel reaped it
    and no descendant was left unverified."""
    return (
        d.get("worker_cleanup_confirmed") is True
        and not d.get("worker_cleanup_pending")
        and not d.get("claude_pid")
        and not d.get("cancel_unverified_pids")
        and not d.get("cancel_inventory_error")
    )


def _ruled_out(root: Path, d: dict[str, Any], *, reaped: bool | None = None) -> tuple[list[int], bool]:
    """The processes `d` owns, and whether a trustworthy inventory shows none. `reaped` (default:
    the record confirms its cleanup) lets `owned_pids` clear an unreadable Windows row that only
    started after the dispatch; an empty, failed or otherwise unread inventory still holds."""
    pids, trusted = owned_pids(root, d, reaped=cleanup_confirmed(d) if reaped is None else reaped)
    return pids, trusted and not pids


def _doubt(d: dict[str, Any]) -> bool:
    """An owned process may have outlived what the record confirms: a cancel could not verify every
    descendant, or the attempt's supervisor died before it could reap what it left."""
    if d.get("cancel_unverified_pids") or d.get("cancel_inventory_error"):
        return True
    sup = d.get("supervisor_pid")
    return bool(d.get("attempt_started")) and sup not in (None, os.getpid()) and not core.is_alive(sup)


def held_areas(root: Path, sid: str, kinds: tuple[str, ...]) -> list[str]:
    """Every lease of `kinds` that `sid` holds, a malformed record's slot included, worktree last."""
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
    d["holdings_held"], d["record_revision"] = current["holdings_held"], current["record_revision"]


def _free(root: Path, sid: str, kinds: tuple[str, ...] = ("repo-slot:", "worktree:")) -> list[str]:
    return [area for area in held_areas(root, sid, kinds) if leases_mod.release(root, area, sid)]


def release(root: Path, d: dict[str, Any]) -> None:
    """Release `d`'s slot, then its worktree lease, holder-checked and best effort; the lease layer
    refuses while cleanup is unconfirmed. When a process `d` owns may have outlived its supervisor
    and a trustworthy inventory cannot rule that out, both stay and the record says why."""
    try:
        if _doubt(d):
            pids, idle = _ruled_out(root, d)
            if not idle:
                _mark_held(root, d, pids)
                return
        lane.tidy(root, d)
        _free(root, f"dispatch:{d['id']}", HELD_AREAS)
    except Exception:  # noqa: BLE001, S110 — a kept holding is retried by `reconcile`
        pass


def abandon(root: Path, d: dict[str, Any]) -> None:
    """Abandon's release (A2): only the worktree and slot leases this dispatch holds, holder-checked.
    It refuses while an owned process may still run, so the holdings stay with the record."""
    if d.get("holdings_held"):
        raise AgentError(_held_message(d))
    _free(root, f"dispatch:{d['id']}")


def run_blocks(root: Path, lease: dict[str, Any]) -> bool:
    """A reverify run's lane protects the run until its checkout is gone and its verifier's
    cleanup and temp root are confirmed gone. Before the run is on the record, its checkout says."""
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
            # A row or binding it cannot read may be this run's.
            if row is None or binding is None:
                return True
            if run_id in (row.get("run_id"), binding.get("run_id")):
                busy = bool(binding) and lane.run_busy(root, binding_of(binding))
                return bool(row.get("cleanup_pending")) or busy
        checkout = lease.get("checkout")
        return bool(checkout) and Path(str(checkout)).exists()
    except Exception:  # noqa: BLE001 — unreadable state cannot free a verifier's holding
        return True


def reverify_take(root: Path, d: dict[str, Any], run_id: str, checkout: Path) -> Taken:
    """Reverify's holdings (lock plan 4b): the dispatch's worktree lease, then a verification lane
    held as `reverify:<id>:<run_id>`, both non-waiting. A busy lane is `lane.LaneBusy`."""
    from .verification_gate import requires_lease

    refuse_cutover(root, d)
    if requires_lease(root, d) or d.get("holdings_held"):
        raise AgentError(f"dispatch {d['id']} has a live worker or unconfirmed cleanup; reverify refused")
    target = {**d, "worktree_key": d.get("worktree_key") or worktree_key(d["worktree"])}
    note, taken = f"reverify {d['id']} {run_id}", Taken()
    try:
        _take_worktree(root, target, note, taken)
        sid = f"{REVERIFY}{d['id']}:{run_id}"
        taken += lane.take_now(root, target, sid, note, marks={"checkout": str(checkout)})[1]
    except BaseException:
        rollback(root, taken)
        raise
    return taken


def reverify_release(root: Path, d: dict[str, Any], run_id: str) -> None:
    """Free the run's lane, then the dispatch's worktree lease, holder-checked."""
    try:
        _free(root, f"{REVERIFY}{d['id']}:{run_id}", ("verify:",))
        _free(root, f"dispatch:{d['id']}")
    except Exception:  # noqa: BLE001, S110 — a kept holding is retried by `reconcile`
        pass


def reverify_held(root: Path, dispatch_id: str) -> dict[str, list[tuple[str, str]]]:
    """Each run's (area, holder) leases by run id: its lane first, the worktree while its note names it."""
    lane_sid, note_of = f"{REVERIFY}{dispatch_id}:", f"reverify {dispatch_id} "
    held: dict[str, list[tuple[str, str]]] = {}
    for lease in leases_mod.list_all(root):
        sid, area, note = (str(lease.get(key) or "") for key in ("session_id", "area", "note"))
        if sid.startswith(lane_sid) and area.startswith("verify:"):
            held.setdefault(sid.removeprefix(lane_sid), []).insert(0, (area, sid))
        elif sid == f"dispatch:{dispatch_id}" and area.startswith("worktree:") and note.startswith(note_of):
            held.setdefault(note.removeprefix(note_of), []).append((area, sid))
    return held


def reverify_free(root: Path, dispatch_id: str, run_id: str) -> bool:
    """Free what a finished run still holds; True once it holds nothing."""
    for area, sid in reverify_held(root, dispatch_id).get(run_id, []):
        leases_mod.release(root, area, sid)
    return run_id not in reverify_held(root, dispatch_id)


def reconcile(root: Path, *, limit: int) -> list[str]:
    """Free what is now provably idle: a `holdings_held` record once a trustworthy inventory shows
    no owned process, and a lane whose holder died once its verifier's cleanup is confirmed.
    Bounded, waits on nothing, and never raises."""
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
    except Exception:  # noqa: BLE001, S110 — a failed pass retries at the next new, collect or start
        pass
    return released


def record_unsettled(root: Path, sid: str) -> bool:
    """Whether a pass-record lane holder's run may still have processes (`verify_run.unsettled`).
    The lease layer keeps such a lane held even after its holder dies (another
    record run took it over)."""
    from .. import verify_record, verify_run

    return verify_run.unsettled(verify_record.store(root) / "runs" / sid.removeprefix(lane.RECORD))


def _record_settled(root: Path, sid: str) -> bool:
    """A dead pass-record holder's lane is free once its run's cleanup is confirmed."""
    from .. import verify_record, verify_run

    run_dir = verify_record.store(root) / "runs" / sid.removeprefix(lane.RECORD)
    return verify_run.settle(verify_record.main_of(root), run_dir)


def preview(root: Path, rp: repos_mod.Repo) -> dict[str, Any]:
    """`new --dry-run`'s view: real slot availability and the retired repo lease, read with no
    guard and no write."""
    limit, held = cap_of(rp), list[dict[str, Any]]()
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
        held = ", ".join(f"{h['slot']} by {h['session_id']} ({h['status']})" for h in slots["held"])
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
    """`cleanup`'s check before removing a worktree: a live holder of its worktree lease, or of the
    retired repo lease, refuses."""
    for area in (worktree_area(short, worktree), f"repo:{short}"):
        lease = leases_mod.get(root, area)
        if lease is not None and leases_mod.status(lease, root) in leases_mod.BLOCKING_STATUSES:
            raise AgentError(leases_mod.refusal(root, area, lease))


def cleanup_take(root: Path, short: str, worktree: Path, sid: str) -> Taken:
    """`cleanup`'s hold on a worktree while Git checks and removes it outside the records guard: its
    worktree lease as `sid`, a non-waiting try, so a resume, reverify or reusing `new` there
    refuses until `rollback` frees it."""
    area = worktree_area(short, worktree)
    leases_mod.take_marked(
        root, area, session_id=sid, pid=os.getpid(), note="cleanup", worktree_key=worktree_key(worktree)
    )
    return [(area, sid)]


def release_slot(root: Path, dispatch_id: str, attempt: int) -> bool:
    """Release a repository slot after confirmed model-process cleanup, while keeping the worktree
    lease for verification or resume. Mark `slot_released` before release so the lease layer
    permits it even while the supervisor lives. Clear the stored slot so resume takes a free one.
    Unconfirmed cleanup keeps the slot held.
    """
    from .state import save
    from .verification_gate import record_guard

    with record_guard(root, dispatch_id) as d:
        if (
            d.get("attempt") != attempt
            or d.get("worker_cleanup_pending")
            or not d.get("worker_cleanup_confirmed")
        ):
            return False
        d["slot_released"] = {"attempt": attempt}
        save(root, d)
        freed = _free(root, f"dispatch:{dispatch_id}", ("repo-slot:",))
        d.pop("slot", None)
        d.pop("slot_schema", None)  # with no slot, a stored schema would read as a broken record
        d["slot_released"] = {"attempt": attempt, "areas": freed}
        save(root, d)
    telemetry_events.transition(root, d, "awaiting-verification")
    return True


def slot_released(d: dict[str, Any]) -> bool:
    """Whether `d`'s current attempt gave its repo slot back after confirmed worker cleanup."""
    return as_dict(d.get("slot_released")).get("attempt") == d.get("attempt") and bool(
        d.get("worker_cleanup_confirmed")
    )


def pending_artifacts(root: Path, short: str) -> list[str]:
    """Dispatches in `short` whose model has finished and given back its slot but which are not
    terminal yet: finished work waiting for verification or acceptance. One whose supervisor died
    is not waiting on a lane but on a collect, so it does not hold back new work (Codex, phase-3
    review: one stale record refused every launch after a restart)."""
    from .state import TERMINAL_STATES, load_all

    return [
        str(d["id"])
        for d in load_all(root)
        if d.get("short") == short
        and d.get("state") not in TERMINAL_STATES
        and as_dict(d.get("slot_released")).get("attempt") == d.get("attempt")
        and core.is_alive(d.get("supervisor_pid"))
    ]


def pending_limit(rp: repos_mod.Repo) -> int:
    """`dispatch.max_pending_verify`, default twice the verification lanes and at least 2."""
    from .lane import lanes_of

    return as_int(rp.dispatch.get("max_pending_verify"), 0) or max(2, 2 * lanes_of(rp))


def refuse_backpressure(root: Path, rp: repos_mod.Repo) -> None:
    """Refuse a new launch while the queue of finished work awaiting verification is full, naming
    it: more workers would only finish into a queue the lanes cannot drain."""
    waiting = pending_artifacts(root, rp.short)
    limit = pending_limit(rp)
    if len(waiting) >= limit:
        raise AgentError(
            f"{len(waiting)} finished dispatches in {rp.short} await verification (limit {limit}: "
            f"{', '.join(waiting)}); launch once one completes, or raise dispatch.max_pending_verify"
        )
