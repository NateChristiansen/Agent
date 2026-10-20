# ruff: noqa
from __future__ import annotations
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from .. import core
from .. import features as features_mod
from .. import leases as leases_mod
from .. import repos as repos_mod
from ..core import AgentError
from .guards import area_guard

if TYPE_CHECKING:
    from ..procs import Proc
GUARD_TIMEOUT_S = 3.0
RECONCILE_LIMIT = 64
INVENTORY_READS = 3
OWNER_PIDS = ("supervisor_pid", "claude_pid")


def worktree_key(path: Path | str) -> str:
    return os.path.realpath(path)


def worktree_area(short: str, path: Path | str) -> str:
    return f"worktree:{short}/{Path(worktree_key(path)).name}"


@contextmanager
def short_guard(root: Path, area: str):
    entered = False
    try:
        with area_guard(root, area, timeout=GUARD_TIMEOUT_S):
            entered = True
            yield
    except AgentError as exc:
        if entered or "is busy" not in str(exc):
            raise
        raise AgentError(
            f"{area} is busy past {GUARD_TIMEOUT_S:g} s; retry after the current operation"
        ) from exc


def git_admin(root: Path, short: str):
    return short_guard(root, f"git-admin:{short}")


def git_admin_short(root: Path, main: Path) -> str:
    try:
        for repo in repos_mod.load_registry(root).repos:
            if repo.abs_path(root) == main.resolve():
                return repo.short
    except Exception:
        pass
    name = "".join((c if c.isascii() and (c.isalnum() or c in "._-") else "-" for c in main.resolve().name))
    return name.lstrip("._-") or "repo"


def refuse_cutover(root: Path, d: dict[str, Any]) -> None:
    short = d["short"]
    lease = leases_mod.get(root, f"repo:{short}")
    holder = str((lease or {}).get("session_id") or "")
    if lease is None or holder == f"dispatch:{d['id']}":
        return
    if leases_mod.status(lease, root) not in leases_mod.BLOCKING_STATUSES:
        return
    if not holder.startswith("dispatch:"):
        raise AgentError(leases_mod.refusal(root, f"repo:{short}", lease))
    if lease.get("lease_protocol") != leases_mod.LEASE_PROTOCOL:
        raise AgentError(
            f"repo:{short} is held by {holder} without lease_protocol {leases_mod.LEASE_PROTOCOL}: a dispatch from older harness code is still running there. Let it finish or collect it first; nothing was changed"
        )
    raise AgentError(
        f"repo:{short} is held by {holder}, a dispatch that runs without a repo slot. Let it finish or collect it first; nothing was changed"
    )


def alloc_guard(root: Path, short: str):
    return short_guard(root, f"dispatch-alloc:{short}")


def allocate(root: Path, feat: features_mod.Feature, rp: repos_mod.Repo, tag: str | None) -> tuple[int, str]:
    from .state import allocate as next_id

    return next_id(root, feat, tag)


def reusable(
    root: Path,
    feat: features_mod.Feature,
    rp: repos_mod.Repo,
    tag: str | None,
    branch: str | None = None,
    *,
    preview_id: str | None = None,
) -> Path | None:
    from .orders import prepared_worktrees
    from .worktrees import reusable_worktree

    if preview_id is None:
        return reusable_worktree(root, feat, rp, tag, branch)
    return reusable_worktree(
        root, feat, rp, tag, branch, excluded=prepared_worktrees(root, excluding=preview_id)
    )


def claim(
    root: Path,
    d: dict[str, Any],
    feat: features_mod.Feature,
    rp: repos_mod.Repo,
    tag: str | None,
    *,
    seq: int,
    reused: Path | None,
    escalated: bool,
) -> None:
    from . import machine
    from .holdings import acquire, cap_of, rollback
    from .state import allocate, dispatches_dir, save

    d["label_claim"] = machine.require_claim(root, tag)
    reconcile_allocations(root)
    refuse_cutover(root, d)
    with alloc_guard(root, rp.short):
        if allocate(root, feat, tag)[0] != seq or (
            not escalated
            and reusable(root, feat, rp, tag, str(d.get("branch") or ""), preview_id=d.get("preview_id"))
            != reused
        ):
            raise AgentError(
                f"a concurrent `dispatch new` took {d['id']} or its worktree; nothing was created, run it again"
            )
        d["worktree_key"] = worktree_key(d["worktree"])
        d["allocator_pid"] = os.getpid()
        taken = acquire(root, d, note=f"dispatch {d['id']}", limit=cap_of(rp))
        try:
            dispatches_dir(root).mkdir(parents=True, exist_ok=True)
            save(root, d)
        except BaseException:
            rollback(root, taken)
            raise


def allocating(d: dict[str, Any]) -> bool:
    if d.get("state") != "queued" or d.get("supervisor_pid") or d.get("terminal_committed"):
        return False
    if d.get("allocation_held"):
        return True
    pid = d.get("allocator_pid")
    return pid is not None and core.is_alive(pid)


def refuse_allocating(d: dict[str, Any], action: str) -> None:
    if allocating(d):
        raise AgentError(
            f"dispatch {d['id']} is still allocating; {action} after it starts or once its allocator exits"
        )


def _inventory() -> list[Proc]:
    from .. import procs

    seen = procs.snapshot()
    for _ in range(INVENTORY_READS - 1):
        if sys.platform == "win32" or not any((p.tag_error for p in seen)):
            break
        seen = procs.snapshot()
    return seen


def owned_pids(root: Path, owner: str | dict[str, Any], *, reaped: bool = False) -> tuple[list[int], bool]:
    from ..proc_tags import resolved_root

    d = owner if isinstance(owner, dict) else {"id": owner}
    try:
        seen, here = (_inventory(), resolved_root(str(root)))
    except Exception:
        return ([], False)
    named = {d.get(key) for key in OWNER_PIDS} - {None, 0, 1}
    groups = set() if sys.platform == "win32" else {d.get("claude_pid")} - {None, 0, 1, os.getpgrp()}

    def ours(p: Proc) -> bool:
        return p.dispatch == d["id"] and resolved_root(p.root) == here

    owned = {p.pid for p in seen if ours(p) or ((p.pid in named or p.pgid in groups) and (not p.dispatch))}
    children: dict[int, list[int]] = {}
    for p in seen:
        if ours(p) or not p.dispatch:
            children.setdefault(p.ppid, []).append(p.pid)
    windows = sys.platform == "win32"
    stack: list[int] = list(owned)
    rows = {p.pid: p for p in seen}
    if windows:
        for gone in (p for p in named if isinstance(p, int) and p not in rows):
            for child in children.get(gone, []):
                if child not in owned and _maybe_after(_born(rows[child]), d):
                    owned.add(child)
                    stack.append(child)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in owned:
                owned.add(child)
                stack.append(child)
    pids = [p.pid for p in seen if p.pid in owned and p.pid != os.getpid()]
    if windows:
        return (pids, bool(seen) and (not any((_maybe_ours(p, owned, rows, d, reaped) for p in seen))))
    return (pids, bool(seen) and (not any((p.tag_error for p in seen))))


def _maybe_ours(
    p: Proc, owned: set[int], rows: dict[int, Proc], d: dict[str, Any], reaped: bool = False
) -> bool:
    if not p.tag_error:
        return False
    node, visited = (p, set[int]())
    while True:
        if node.pid in owned:
            return True
        if node.dispatch:
            return False
        visited.add(node.pid)
        parent = rows.get(node.ppid)
        if parent is None or parent.pid in visited or (not _surely_before(parent, node)):
            return not reaped and _maybe_after(_born(node), d)
        node = parent


def _surely_before(parent: Proc, child: Proc) -> bool:
    a, b = (_born(parent), _born(child))
    return a is not None and b is not None and (a < b)


def _maybe_after(born: float | None, d: dict[str, Any]) -> bool:
    try:
        since = datetime.fromisoformat(str(d.get("started") or "")).timestamp()
    except ValueError:
        return True
    return born is None or born >= since


def _born(p: Proc) -> float | None:
    try:
        return int(p.started) / 10000000 - 11644473600
    except ValueError:
        return p.created


def release_held(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    from .state import save
    from .verification_gate import record_guard

    if not allocating(d) or not d.get("allocation_held"):
        return d
    pids, trusted = owned_pids(root, d)
    if pids or not trusted:
        return d
    with record_guard(root, d["id"]) as current:
        if current.get("allocation_held") == d["allocation_held"] and (not current.get("supervisor_pid")):
            current["allocation_released"] = {**current.pop("allocation_held"), "reason": "no owned process"}
            save(root, current)
        return current


def _remove_untouched(root: Path, d: dict[str, Any]) -> bool:
    from .state import file, load

    try:
        with area_guard(root, f"dispatch-records:{d['short']}", timeout=GUARD_TIMEOUT_S):
            current = load(root, d["id"])
            if current.get("record_revision") == d.get("record_revision") and current["state"] == "queued":
                file(root, d["id"], ".json").unlink()
                return True
    except Exception:
        pass
    return False


def fail(root: Path, d: dict[str, Any], stage: str, exc: BaseException) -> None:
    from .state import load, release_repo_lease, save
    from .verification_gate import record_guard

    if stage in {"work order", "checkout"} and _remove_untouched(root, d):
        release_repo_lease(root, d)
        return
    try:
        record = load(root, d["id"])
        if record.get("allocator_pid") != os.getpid():
            return
        pids, trusted = owned_pids(root, record) if stage == "supervisor start" else ([], True)
        held = bool(pids) or not trusted
        with record_guard(root, d["id"]) as current:
            if current.get("allocator_pid") != os.getpid() or current.get("supervisor_pid"):
                return
            current["allocator_pid"] = None
            current["error"] = f"dispatch new failed at {stage} ({type(exc).__name__}): {exc}"
            if held:
                why = (
                    "a dispatch process may still run"
                    if pids
                    else "the process inventory was not trustworthy"
                )
                current["allocation_held"] = {"stage": stage, "pids": pids, "reason": why}
            save(root, current)
    except Exception:
        return
    if not held:
        release_repo_lease(root, current)


def reconcile_allocations(root: Path, *, limit: int = RECONCILE_LIMIT) -> list[str]:
    from .holdings import reconcile
    from .ids import ID_RE
    from .state import file

    released: list[str] = []
    try:
        reservations = [
            lease
            for lease in leases_mod.list_all(root)
            if str(lease.get("area") or "").startswith(("worktree:", "repo-slot:"))
            and str(lease.get("session_id") or "").startswith("dispatch:")
        ]
        for lease in reservations[:limit]:
            dispatch_id = str(lease["session_id"]).removeprefix("dispatch:")
            pid = lease.get("pid")
            if pid is None or core.is_alive(pid) or (not ID_RE.fullmatch(dispatch_id)):
                continue
            if file(root, dispatch_id, ".json").exists():
                continue
            if leases_mod.release(root, str(lease["area"]), str(lease["session_id"])):
                released.append(str(lease["area"]))
    except Exception:
        pass
    return released + reconcile(root, limit=limit)
