"""a dispatch between `claim` and its supervisor's handoff names its allocating
process, and every destructive lifecycle step treats it as live while that process is. Each
pre-handoff failure in `new` closes the allocation out, and the records guard keeps its own
acquisition deadline (K0 addendum A3)."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_cli import dispatch, leases, procs
from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, guards, lifecycle, notice_delivery, state, supervisor
from agent_cli.dispatch import verification_gate as gate
from dispatch_helpers import DEAD_PID, _new_fast, droot  # noqa: F401
from test_dispatch_alloc import _no_detach

STAGES = ("checkout", "setup", "session", "worker files", "handoff", "supervisor start")
REAL_SNAPSHOT = procs.snapshot  # read at import, before conftest's autouse stub replaces it


def _only_id(root: Path) -> str:
    [dispatch_id] = [p.stem for p in (root / ".agent/dispatches").glob("001-*.json") if "." not in p.stem]
    return dispatch_id


def _holdings(root: Path, dispatch_id: str) -> list[str]:
    return sorted(
        str(lease["area"])
        for lease in leases.list_all(root)
        if lease.get("session_id") == f"dispatch:{dispatch_id}"
    )


def _at(monkeypatch: pytest.MonkeyPatch, root: Path, stage: str, action) -> None:
    """Run `action(dispatch_id)` inside `new` at `stage`, before the stage's real step."""
    if stage == "supervisor start":

        class Spawn:
            pid = DEAD_PID

            def __init__(self, *args, **kwargs):
                action(_only_id(root))

        monkeypatch.setattr(supervisor, "popen", Spawn)
        return
    module, name = {
        "checkout": (lifecycle, "ensure_worktree"),
        "setup": (lifecycle.telemetry_events, "setup_timing"),
        "session": (lifecycle.sessions, "set_record"),
        "worker files": (lifecycle, "write_worker_files"),
        "handoff": (lifecycle, "write_handoff"),
    }[stage]
    real = getattr(module, name)

    def paused(*args, **kwargs):
        action(_only_id(root))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, paused)


def _assert_live(root: Path, dispatch_id: str) -> None:
    held = _holdings(root, dispatch_id)
    assert held == [
        "repo-slot:alpha/0",
        allocation.worktree_area("alpha", dispatch.load(root, dispatch_id)["worktree"]),
    ]
    assert dispatch.collect(root, dispatch_id=dispatch_id)[0]["state"] == "queued"
    assert [row["state"] for row in dispatch.collect(root) if row["id"] == dispatch_id] == ["queued"]
    assert gate.settle(root, dispatch_id, cancel=True) is False
    with pytest.raises(AgentError, match="queued; cancel it first"):
        dispatch.abandon(root, dispatch_id=dispatch_id, note="stale")
    with pytest.raises(AgentError, match="still running"):
        dispatch.cleanup(root, feature="1", force=True)
    assert allocation.reconcile_allocations(root) == []
    assert all(leases.release(root, area, f"dispatch:{dispatch_id}") is False for area in held)
    record = dispatch.load(root, dispatch_id)
    assert record["state"] == "queued" and record["allocator_pid"] == os.getpid()
    assert not record.get("terminal_committed") and _holdings(root, dispatch_id) == held


@pytest.mark.parametrize("stage", STAGES)
def test_a_live_allocation_is_never_collected(droot: Path, monkeypatch, stage):
    _no_detach(monkeypatch)
    seen: list[str] = []
    _at(
        monkeypatch,
        droot,
        stage,
        lambda dispatch_id: _assert_live(droot, dispatch_id) or seen.append(dispatch_id),
    )
    d = _new_fast(droot, watch=False)
    assert seen == [d["id"]]
    record = dispatch.load(droot, d["id"])
    assert record["supervisor_pid"] == DEAD_PID and record["allocator_pid"] is None


def _snapshot(root: Path, dispatch_id: str, worktree: Path) -> tuple:
    lease_dir = root / ".agent/leases"
    return (
        (root / f".agent/dispatches/{dispatch_id}.json").read_bytes(),
        sorted((p.name, p.read_bytes()) for p in lease_dir.glob("*.json")),
        worktree.exists(),
        subprocess.run(["git", "status", "--porcelain"], cwd=worktree, capture_output=True).stdout
        if worktree.exists()
        else b"",
    )


@pytest.mark.parametrize("stage", ["checkout", "worker files", "supervisor start"])
def test_cancel_refuses_a_live_allocation_and_changes_nothing(droot: Path, monkeypatch, stage):
    _no_detach(monkeypatch)

    def cancel(dispatch_id: str) -> None:
        worktree = Path(dispatch.load(droot, dispatch_id)["worktree"])
        before = _snapshot(droot, dispatch_id, worktree)
        message = (
            f"dispatch {dispatch_id} is still allocating; cancel after it starts or once its allocator exits"
        )
        with pytest.raises(AgentError) as refused:
            dispatch.cancel(droot, dispatch_id=dispatch_id)
        assert str(refused.value) == message
        assert _snapshot(droot, dispatch_id, worktree) == before

    _at(monkeypatch, droot, stage, cancel)
    assert _new_fast(droot, watch=False)["state"] == "queued"


def _die(root: Path, dispatch_id: str) -> None:
    """The allocating process dies here: its pid goes, and nothing in `new` runs after it."""
    with gate.record_guard(root, dispatch_id) as current:
        current["allocator_pid"] = DEAD_PID
        state.save(root, current)
    sid, key = f"dispatch:{dispatch_id}", current["worktree_key"]
    leases.take_marked(root, "repo-slot:alpha/0", session_id=sid, pid=DEAD_PID)
    leases.take_marked(
        root, allocation.worktree_area("alpha", key), session_id=sid, pid=DEAD_PID, worktree_key=key
    )
    raise SystemExit("allocator died")


@pytest.mark.parametrize("stage", ["checkout", "handoff"])
def test_a_dead_allocator_is_collected_failed_once(droot: Path, monkeypatch, stage):
    _no_detach(monkeypatch)
    _at(monkeypatch, droot, stage, lambda dispatch_id: _die(droot, dispatch_id))
    with pytest.raises(SystemExit):
        _new_fast(droot, watch=False)
    dispatch_id = _only_id(droot)
    assert dispatch.load(droot, dispatch_id)["state"] == "queued" and len(_holdings(droot, dispatch_id)) == 2
    [row] = dispatch.collect(droot, dispatch_id=dispatch_id)
    assert row["state"] == "failed" and leases.list_all(droot) == []
    revision = dispatch.load(droot, dispatch_id)["record_revision"]
    dispatch.collect(droot)
    dispatch.collect(droot, dispatch_id=dispatch_id)
    assert dispatch.load(droot, dispatch_id)["record_revision"] == revision


def test_the_supervisor_pid_ends_the_allocator_protection(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    with gate.record_guard(droot, d["id"]) as current:
        assert current["allocator_pid"] is None and current["supervisor_pid"] == DEAD_PID
        current["allocator_pid"] = os.getpid()
        state.save(droot, current)
    [row] = dispatch.collect(droot, dispatch_id=d["id"])
    assert row["state"] == "failed" and leases.list_all(droot) == []


def _proc(pid: int, dispatch: str = "", root: str = "") -> procs.Proc:
    return procs.Proc(
        pid=pid, ppid=1, pgid=pid, elapsed="00:01", command="python", dispatch=dispatch, root=root
    )


def _boom(stage: str):
    def fail(*args, **kwargs):
        raise (OSError if stage == "supervisor start" else AgentError)(f"boom at {stage}")

    return fail


@pytest.mark.parametrize("stage", STAGES)
def test_a_failure_before_handoff_closes_the_allocation(droot: Path, monkeypatch, stage):
    _no_detach(monkeypatch)
    if stage == "supervisor start":
        monkeypatch.setattr(supervisor, "popen", _boom(stage))
        monkeypatch.setattr(procs, "snapshot", lambda: [_proc(os.getpid())])
    else:
        _at(monkeypatch, droot, stage, _boom(stage))
    with pytest.raises((AgentError, OSError), match=f"boom at {stage}"):
        _new_fast(droot, watch=False)
    assert leases.list_all(droot) == []
    if stage == "checkout":
        assert not list((droot / ".agent/dispatches").glob("001-*.json"))
        return
    dispatch_id = _only_id(droot)
    record = dispatch.load(droot, dispatch_id)
    assert record["state"] == "queued" and record["allocator_pid"] is None
    assert f"dispatch new failed at {stage}" in record["error"] and "allocation_held" not in record
    [row] = dispatch.collect(droot, dispatch_id=dispatch_id)
    assert row["state"] == "failed"
    revision = dispatch.load(droot, dispatch_id)["record_revision"]
    dispatch.collect(droot, dispatch_id=dispatch_id)
    assert dispatch.load(droot, dispatch_id)["record_revision"] == revision


def _inventory(monkeypatch: pytest.MonkeyPatch, mode: str, droot: Path, pid: int) -> None:
    """`procs.snapshot` as `mode` describes it: the spawned process found by its tags, no rows,
    a raised inventory, or the real POSIX snapshot whose `ps -E` tag read returned nothing or
    only the manager's row."""
    if mode == "found":
        rows = [_proc(os.getpid()), _proc(pid, dispatch="001-mac-01", root=str(droot))]
        monkeypatch.setattr(procs, "snapshot", lambda: rows)
    elif mode == "empty":
        monkeypatch.setattr(procs, "snapshot", lambda: [])
    elif mode == "raises":
        monkeypatch.setattr(procs, "snapshot", lambda: 1 / 0)
    else:
        tagged = "" if mode == "untagged" else f"{os.getpid()} python\n"
        _real_ps(monkeypatch, tagged, [os.getpid(), pid])


def _real_ps(monkeypatch: pytest.MonkeyPatch, tagged: str, pids: list[int]) -> None:
    """The production POSIX snapshot over `ps` output: `tagged` for the `-E` read, and a
    command row for each of `pids`."""
    lines = "".join(f"{p} 1 {p} 00:01 python\n" for p in pids)
    monkeypatch.setattr(procs, "_ps", lambda args: tagged if "-E" in args else lines)
    monkeypatch.setattr(procs, "snapshot", REAL_SNAPSHOT)


@pytest.mark.parametrize("mode", ["found", "empty", "raises", "untagged", "partial"])
def test_an_uncertain_supervisor_start_stays_held_until_a_trustworthy_inventory(
    droot: Path, monkeypatch, mode
):
    sleeper = subprocess.Popen(["sleep", "30"])

    class Spawned:
        """The supervisor starts, then `detach`'s own save of its pid goes stale."""

        pid = sleeper.pid

        def __init__(self, *args, **kwargs):
            dispatch_id = _only_id(droot)
            state.save(droot, dispatch.load(droot, dispatch_id))

    monkeypatch.setattr(supervisor, "popen", Spawned)
    _inventory(monkeypatch, mode, droot, sleeper.pid)
    try:
        with pytest.raises(AgentError, match="stale dispatch record"):
            _new_fast(droot, watch=False)
        dispatch_id = _only_id(droot)
        record = dispatch.load(droot, dispatch_id)
        held = record["allocation_held"]
        assert record["allocator_pid"] is None and record["supervisor_pid"] is None
        assert held["stage"] == "supervisor start" and held["pids"] == (
            [sleeper.pid] if mode == "found" else []
        )
        assert held["reason"] == (
            "a dispatch process may still run"
            if mode == "found"
            else "the process inventory was not trustworthy"
        )
        holdings = _holdings(droot, dispatch_id)
        assert len(holdings) == 2 and sleeper.poll() is None
        assert dispatch.collect(droot, dispatch_id=dispatch_id)[0]["state"] == "queued"
        assert [row["state"] for row in dispatch.collect(droot) if row["id"] == dispatch_id] == ["queued"]
        assert gate.settle(droot, dispatch_id, cancel=True) is False
        with pytest.raises(AgentError, match="still allocating"):
            dispatch.cancel(droot, dispatch_id=dispatch_id)
        assert all(leases.release(droot, area, f"dispatch:{dispatch_id}") is False for area in holdings)
        with pytest.raises(AgentError, match="still running"):
            dispatch.cleanup(droot, feature="1", force=True)
        record = dispatch.load(droot, dispatch_id)
        assert record["state"] == "queued" and record["allocation_held"] == held
        assert _holdings(droot, dispatch_id) == holdings
    finally:
        sleeper.kill()
        sleeper.wait()
    if mode == "partial" and sys.platform != "win32":
        _real_ps(monkeypatch, f"{os.getpid()} python\n", [os.getpid()])
    else:
        monkeypatch.setattr(procs, "snapshot", lambda: [_proc(os.getpid())])
    [row] = dispatch.collect(droot, dispatch_id=dispatch_id)
    assert row["state"] == "failed" and leases.list_all(droot) == []
    revision = dispatch.load(droot, dispatch_id)["record_revision"]
    dispatch.collect(droot)
    dispatch.collect(droot, dispatch_id=dispatch_id)
    assert dispatch.load(droot, dispatch_id)["record_revision"] == revision


def test_each_guard_has_its_acquisition_deadline(droot: Path, monkeypatch):
    rows: list[tuple[str, float | None]] = []
    real = guards.area_guard

    @contextmanager
    def recording(root, area, **kw):
        rows.append((area.split(":", 1)[0], kw.get("timeout")))
        with real(root, area, **kw):
            yield

    for module in (guards, gate, allocation):
        monkeypatch.setattr(module, "area_guard", recording)
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    deadlines = {area: {timeout for a, timeout in rows if a == area} for area, _ in rows}
    assert deadlines["dispatch-alloc"] == {3.0} and deadlines["git-admin"] == {3.0}
    assert not hasattr(state, "RECORDS_GUARD_TIMEOUT_S")
    assert deadlines["dispatch-records"] == {allocation.GUARD_TIMEOUT_S}
    rows.clear()
    notice_delivery.claim(droot, d["id"], "no-such-notice")
    assert ("dispatch-records", notice_delivery.GUARD_TIMEOUT_S) in rows
