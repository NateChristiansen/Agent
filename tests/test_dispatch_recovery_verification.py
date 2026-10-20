"""Real synthetic parent death and repeatable cleanup recovery at the dispatch boundary."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import dispatch, leases, templates
from agent_cli.dispatch import recovery, terminal, verification, verification_process
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load, save
from dispatch_helpers import CHILD_ENV, _set_alpha_dispatch, droot  # noqa: F401
from test_dispatch_verification import ready, wait_for  # noqa: F401


@pytest.mark.parametrize("when", ["during", "after"])
def test_parent_sigkill_collect_only_accepts_completed_evidence(ready, when):
    root, d = ready
    marker = root / "child-ready"
    package = Path(__file__).resolve().parents[1] / "src"
    if when == "during":
        command = f"touch {shlex.quote(str(marker))}; sleep 20"
        _set_alpha_dispatch(
            root, verify=[{"command": command, "timeout_seconds": 25}], verify_timeout_seconds=30
        )
    code = """import os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load,save
root=Path(sys.argv[2])
d=load(root,'001-mac-01'); d['supervisor_pid']=os.getpid(); save(root,d)
gate.execute(root,'001-mac-01',1)
Path(sys.argv[3]).touch()
time.sleep(30)
"""
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(package), str(root), str(marker)],
        env=CHILD_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for(marker.exists)
        proc.kill()
        proc.wait(timeout=5)
        value = gate.metadata(load(root, d["id"]))
        assert value is not None
        binding, _ = value
        wait_for(lambda: not verification.load_status(root, binding).requires_lease)
        result = recovery.collect(root, dispatch_id=d["id"])[0]
        assert result["state"] == ("blocked" if when == "during" else "done")
        assert leases.get(root, "repo-slot:alpha/0") is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        gate.settle(root, d["id"], cancel=True)


def test_repeat_cancel_reaps_worker_even_when_first_cancel_already_recorded(ready):
    root, d = ready
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        env={"PATH": os.defpath},
        start_new_session=True,
    )
    try:
        d.update(
            state="cancelled", worker_cleanup_pending=True, claude_pid=proc.pid, stop_reason="cleanup_failed"
        )
        if sys.platform == "win32":
            # Windows ends a jobless worker by pid only while its supervisor is alive;
            # tests/test_dispatch_verification.py covers the dead-supervisor case.
            d["supervisor_pid"] = os.getpid()
        save(root, d)
        result = recovery.cancel(root, dispatch_id=d["id"])
        proc.wait(timeout=5)
        assert result["state"] == "cancelled" and result["terminal_committed"]
        assert result["worker_cleanup_confirmed"] is True
        assert leases.get(root, "repo-slot:alpha/0") is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def _assert_finalized(root, result, lease_held):
    assert result["terminal_committed"] is not lease_held and result["state"] == "cancelled"
    assert result.get("verification_cleanup_pending") is lease_held
    assert leases.get(root, "repo-slot:alpha/0") is not None
    assert lease_held or result["holdings_held"]


@pytest.mark.parametrize("lease_held", [False, True])
def test_cancel_finalizes_and_reports_unverified_descendant(ready, monkeypatch, lease_held):
    root, d = ready
    d["claude_pid"] = 728
    save(root, d)
    # 728 is a made-up worker pid: the real kill_group would SIGKILL that process group on the host.
    monkeypatch.setattr(verification_process, "kill_group", lambda pgid: True)
    monkeypatch.setattr(recovery, "await_worker_stop", lambda root, ident: False)
    monkeypatch.setattr(recovery, "kill_group", lambda pid, *, root, dispatch_id: [2253])
    monkeypatch.setattr(terminal, "requires_lease", lambda root, d: lease_held)

    result = recovery.cancel(root, dispatch_id=d["id"])

    _assert_finalized(root, result, lease_held)
    assert result["cancel_unverified_pids"] == [2253]
    assert "cancel incomplete: unverified PIDs [2253]" in result["error"]


@pytest.mark.parametrize("lease_held", [False, True])
def test_cancel_finalizes_when_windows_inventory_fails(ready, monkeypatch, lease_held):
    root, d = ready
    d["claude_pid"] = 728
    save(root, d)
    # 728 is a made-up worker pid: the real kill_group would SIGKILL that process group on the host.
    monkeypatch.setattr(verification_process, "kill_group", lambda pgid: True)
    monkeypatch.setattr(recovery, "await_worker_stop", lambda root, ident: False)

    def fail_inventory(pid, *, root, dispatch_id):
        raise OSError("Toolhelp snapshot failed")

    monkeypatch.setattr(recovery, "kill_group", fail_inventory)
    monkeypatch.setattr(terminal, "requires_lease", lambda root, d: lease_held)

    result = recovery.cancel(root, dispatch_id=d["id"])

    _assert_finalized(root, result, lease_held)
    assert "Toolhelp snapshot failed" in result["error"]


def test_missing_worker_identity_never_invents_cleanup(ready):
    root, d = ready
    d.update(state="cancelled", worker_cleanup_pending=True, claude_pid=None)
    save(root, d)
    assert not gate.settle(root, d["id"], cancel=True, timeout=0)
    assert dispatch.collect(root, dispatch_id=d["id"])[0]["state"] == "cancelled"
    assert not load(root, d["id"]).get("terminal_committed")
    assert leases.get(root, "repo-slot:alpha/0") is not None


def test_cancel_cleanup_timeout_remains_visible_in_summary_and_handoff(ready, monkeypatch):
    from agent_cli.dispatch import state
    from agent_cli.dispatch.handoffs import handoff_ask

    root, d = ready
    d.update(worker_cleanup_pending=True, claude_pid=None)
    save(root, d)
    plan = root / "features/001-alpha-work/plan.md"
    plan_before = plan.read_bytes()
    actual = gate.settle
    monkeypatch.setattr(
        gate, "settle", lambda root, ident, cancel: actual(root, ident, cancel=cancel, timeout=0)
    )
    result = recovery.cancel(root, dispatch_id=d["id"])
    assert result["state"] == "cancelled" and result["verification_cleanup_pending"]
    assert state.summarize(result)["cleanup_pending"] is True
    assert state.is_live(result, {result["feature"]})
    assert "cleanup incomplete" in str(handoff_ask(result))
    handoff = (root / result["handoff"]).read_text()
    assert "Cleanup incomplete" in handoff and "lease is retained" in handoff
    meta, _ = dispatch.parse_handoff(root / result["handoff"])
    assert meta["status"] == "cancelled" and "cleanup incomplete" in meta["ask"] and meta["live"] is True
    assert plan.read_bytes() == plan_before and templates.HANDOFFS_VIEW in plan.read_text()
    assert leases.get(root, "repo-slot:alpha/0") is not None
