"""the worktree lease and repo slot stay held while cleanup is unconfirmed or an
owned process is live, whatever the supervisor's pid or the lease's touch says; a release that
cannot rule a survivor out keeps them until the reconciler can. Reverify's interim slot (K0
addendum A1) lives here too."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import core, dispatch, leases, procs
from agent_cli.core import AgentError
from agent_cli.dispatch import holdings, recovery, state, verification
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch import verification_gate as gate
from dispatch_helpers import (  # noqa: F401
    CHILD_ENV,
    DEAD_PID,
    _new_fast,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
    settle,
)
from test_dispatch_alloc import _no_detach
from test_dispatch_alloc_live import _proc
from test_dispatch_reverify import _blocked_by_dirt
from test_dispatch_verification import ready, wait_for  # noqa: F401

SLOT0, LANE = "repo-slot:alpha/0", "verify:alpha"


def _held_by(root: Path, sid: str) -> list[str]:
    return sorted(str(x["area"]) for x in leases.list_all(root) if x.get("session_id") == sid)


def _edit(root: Path, dispatch_id: str, **fields) -> dict:
    with gate.record_guard(root, dispatch_id) as current:
        current.update(fields)
        state.save(root, current)
    return current


def _orphaned_attempt(root: Path, monkeypatch) -> dict:
    """A launched attempt whose supervisor died after its worker stopped cleanly."""
    _no_detach(monkeypatch)
    d = _new_fast(root, watch=False)
    return _edit(
        root,
        d["id"],
        state="running",
        attempt_started=core.iso(core.now()),
        supervisor_pid=DEAD_PID,
        worker_cleanup_confirmed=True,
        stop_reason="result",
    )


def _refused(root: Path, area: str) -> None:
    with pytest.raises(AgentError, match="is held by"):
        leases.take(root, area, session_id="peer", pid=os.getpid())


def test_unconfirmed_worker_cleanup_keeps_the_holdings_past_a_dead_holder(droot: Path, monkeypatch):
    d = _orphaned_attempt(droot, monkeypatch)
    _edit(droot, d["id"], stop_reason="cleanup_failed", worker_cleanup_confirmed=False)
    for area in _held_by(droot, f"dispatch:{d['id']}"):
        lease = leases.get(droot, area) or {}
        (droot / ".agent/leases" / f"{leases.area_key(area)}.json").write_text(
            json.dumps({**lease, "pid": DEAD_PID})
        )
        assert leases.status(leases.get(droot, area), droot) == "live"
        _refused(droot, area)
        assert not leases.release(droot, area, f"dispatch:{d['id']}")


def test_a_dead_supervisor_with_a_live_descendant_keeps_them_until_it_stops(droot: Path, monkeypatch):
    _set_alpha_dispatch(droot, max_concurrent=1)
    d = _orphaned_attempt(droot, monkeypatch)
    child = _proc(4242, dispatch=d["id"], root=str(droot))
    monkeypatch.setattr(procs, "snapshot", lambda: [_proc(os.getpid()), child])
    [row] = dispatch.collect(droot, dispatch_id=d["id"])
    record = dispatch.load(droot, d["id"])
    assert row["state"] == "failed" and record["holdings_held"]["pids"] == [4242]
    assert _held_by(droot, f"dispatch:{d['id']}") and leases.status(leases.get(droot, SLOT0), droot) == "live"
    settle(droot, d)
    with pytest.raises(AgentError, match="repo-slot:alpha/0 is held by"):
        _new_fast(droot, watch=False)
    with pytest.raises(AgentError, match="keeps its worktree lease and repo slot"):
        dispatch.abandon(droot, dispatch_id=d["id"], note="stale")
    monkeypatch.setattr(procs, "snapshot", lambda: [_proc(os.getpid())])
    later = _new_fast(droot, watch=False)
    assert later["slot"] == 0 and _held_by(droot, f"dispatch:{d['id']}") == []
    assert "holdings_held" not in dispatch.load(droot, d["id"])
    assert dispatch.load(droot, d["id"])["holdings_released"]["reason"] == "no owned process"


@pytest.mark.parametrize("inventory", ["empty", "raises", "partial"])
def test_unknown_cleanup_keeps_them_until_a_trustworthy_inventory(droot: Path, monkeypatch, inventory):
    d = _orphaned_attempt(droot, monkeypatch)
    rows = {"empty": lambda: [], "raises": lambda: 1 / 0}.get(
        inventory, lambda: [_proc(os.getpid()), procs.Proc(7, 1, 7, "0:01", "x", tag_error="tag read failed")]
    )
    monkeypatch.setattr(procs, "snapshot", rows)
    dispatch.collect(droot, dispatch_id=d["id"])
    dispatch.collect(droot)
    if inventory == "partial" and sys.platform == "win32":
        # Windows reads no environment from protected processes, so an unreadable row outside a
        # reaped worker's tree is another session's (`allocation.owned_pids`): it frees them.
        assert "holdings_held" not in dispatch.load(droot, d["id"])
        assert _held_by(droot, f"dispatch:{d['id']}") == []
        return
    assert (
        dispatch.load(droot, d["id"])["holdings_held"]["reason"]
        == "the process inventory was not trustworthy"
    )
    assert len(_held_by(droot, f"dispatch:{d['id']}")) == 2
    monkeypatch.setattr(procs, "snapshot", lambda: [_proc(os.getpid())])
    dispatch.collect(droot)
    assert _held_by(droot, f"dispatch:{d['id']}") == []


def test_an_expired_touch_with_a_live_app_server_stays_held(droot: Path, monkeypatch):
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        d = _orphaned_attempt(droot, monkeypatch)
        _edit(droot, d["id"], provider="codex", claude_pid=server.pid, codex_thread_id="thread-1")
        monkeypatch.setenv("AGENT_NOW", "2099-01-01T00:00:00+00:00")
        for area in _held_by(droot, f"dispatch:{d['id']}"):
            assert leases.status(leases.get(droot, area), droot) == "live"
            _refused(droot, area)
    finally:
        server.kill()
        server.wait()


def test_a_resumable_thread_with_no_live_process_holds_nothing(droot: Path, fake_claude, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    _edit(droot, d["id"], provider="codex", codex_thread_id="thread-1", claude_pid=DEAD_PID)
    dispatch.cancel(droot, dispatch_id=d["id"])
    record = dispatch.load(droot, d["id"])
    assert record["codex_thread_id"] == "thread-1" and record["terminal_committed"]
    assert _held_by(droot, f"dispatch:{d['id']}") == [] and "holdings_held" not in record


def test_the_reconciler_never_takes_a_short_guard_while_waiting(droot: Path, monkeypatch):
    d = _orphaned_attempt(droot, monkeypatch)
    _edit(droot, d["id"], holdings_held={"pids": [], "reason": "the process inventory was not trustworthy"})
    monkeypatch.setattr(procs, "snapshot", lambda: [])
    assert holdings.reconcile(droot, limit=8) == []
    monkeypatch.setattr(holdings, "owned_pids", lambda root, ident: 1 / 0)
    assert holdings.reconcile(droot, limit=8) == []


def _interim(root: Path) -> list[dict]:
    return [x for x in leases.list_all(root) if str(x.get("session_id")).startswith("reverify:")]


def test_reverify_takes_the_lane_as_a_named_run_beside_a_live_worker_excludes_resume_and_releases_it(
    ready, monkeypatch
):
    """The first run covers three things: the lane is held under the reverify run's own name
    while it runs and released after, a live worker's slot elsewhere is left alone, and a resume
    during it is refused. The second run, of the same dispatch, takes a second lane when the
    first is held."""
    root, d = ready
    _blocked_by_dirt(root, d)
    leases.take_marked(root, SLOT0, session_id="dispatch:001-mac-09", pid=os.getpid())
    seen: list[list[dict]] = []
    refusals: list[str] = []
    real = verification.launch_prepared

    def resume_during(prepared):
        seen.append(_interim(root))
        try:
            dispatch.resume(root, dispatch_id=d["id"], answer="again", watch=False)
        except AgentError as exc:
            refusals.append(str(exc))
        return real(prepared)

    with monkeypatch.context() as m:
        m.setattr(verification, "launch_prepared", resume_during)
        row = reverify_mod.reverify(root, d["id"])
    assert row["result"]["status"] == "verified"
    [held] = [x for x in seen[0] if x["area"] == LANE]
    assert held["session_id"] == f"reverify:{d['id']}:{row['binding']['run_id']}"
    assert held["lease_protocol"] == 2 and held["checkout"] == row["checkout"]
    assert (leases.get(root, SLOT0) or {}).get("session_id") == "dispatch:001-mac-09"
    assert leases.get(root, LANE) is None, "the lane is released after the run"
    assert dispatch.load(root, d["id"])["slot"] == 0 and _held_by(root, f"dispatch:{d['id']}") == []
    assert len(refusals) == 1 and "cleanup is incomplete" in refusals[0]
    assert dispatch.load(root, d["id"])["state"] == "blocked"

    _set_alpha_dispatch(root, max_concurrent_verify=2)
    leases.take_marked(root, LANE, session_id="dispatch:001-mac-09", pid=os.getpid())
    areas: list[str] = []
    monkeypatch.setattr(
        verification, "launch_prepared", lambda p: areas.extend(x["area"] for x in _interim(root)) or real(p)
    )
    row = reverify_mod.reverify(root, d["id"])
    assert row["result"]["status"] == "verified" and areas == ["verify:alpha/1"]
    assert (leases.get(root, LANE) or {}).get("session_id") == "dispatch:001-mac-09"


CRASH = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_cli.dispatch import reverify
reverify.reverify(Path(sys.argv[2]), sys.argv[3])
"""


def test_a_crashed_reverify_keeps_its_lane_until_cleanup_is_confirmed(ready):
    root, d = ready
    _blocked_by_dirt(root, d)
    marker = root / "verifier-started"
    _set_alpha_dispatch(
        root,
        verify=[{"command": f"touch {shlex.quote(str(marker))}; sleep 20", "timeout_seconds": 25}],
        verify_timeout_seconds=30,
    )
    package = Path(__file__).resolve().parents[1] / "src"
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", CRASH, str(package), str(root), d["id"]],
        env=CHILD_ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for(marker.exists, timeout=30)
        proc.kill()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    [held] = _interim(root)
    assert held["area"] == LANE and not core.is_alive(held["pid"])
    _refused(root, LANE)
    assert holdings.reconcile(root, limit=8) == []
    run = dispatch.load(root, d["id"])["reverify_runs"][-1]
    binding = reverify_mod.Binding(**run["binding"])
    wait_for(lambda: not verification.load_status(root, binding).requires_lease, timeout=30)
    subprocess.run(["git", "worktree", "remove", "--force", run["checkout"]], cwd=d["worktree"], check=True)
    with gate.record_guard(root, d["id"]) as current:
        current["reverify_runs"][-1]["cleanup_pending"] = False
        state.save(root, current)
    assert holdings.reconcile(root, limit=8) == [LANE]
    assert _interim(root) == [] and not (root / run["run_area"] / "pytest-tmp").exists()
    assert (root / run["run_area"] / "lifecycle.json").exists()


def test_lease_clear_keeps_a_reverify_lane_that_protects_a_checkout(droot: Path, tmp_path):
    from agent_cli import lease_clear

    checkout = tmp_path / "tree"
    checkout.mkdir()
    sid = "reverify:001-mac-01:" + "a" * 32
    leases.take_marked(droot, LANE, session_id=sid, pid=DEAD_PID, marks={"checkout": str(checkout)})
    assert lease_clear.clear(droot, sid, confirm=True)["kept"] == [LANE]
    checkout.rmdir()
    assert lease_clear.clear(droot, sid, confirm=True)["cleared"] == [LANE]


def test_cleanup_refuses_a_worktree_whose_lease_is_live(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    holdings.release(droot, _edit(droot, d["id"], state="done", terminal_committed=True))
    assert recovery.collect(droot) == [] and _held_by(droot, f"dispatch:{d['id']}") == []
    leases.take_marked(
        droot, holdings.worktree_area("alpha", d["worktree"]), session_id="someone", pid=os.getpid()
    )
    with pytest.raises(AgentError, match="is held by someone"):
        dispatch.cleanup(droot, feature="1", force=True)
    assert Path(d["worktree"]).exists()
