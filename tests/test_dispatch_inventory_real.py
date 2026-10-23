"""the real POSIX `ps` inventory, not a stub. A quiet machine's
snapshot is trustworthy, so a crashed supervisor's holdings are freed, and a live descendant
whose environment `ps -E` cannot show (Apple platform binaries) still counts as owned."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_cli import dispatch, procs
from agent_cli.dispatch import allocation, holdings
from dispatch_helpers import _new_fast, droot, settle  # noqa: F401
from test_dispatch_alloc_live import REAL_SNAPSHOT
from test_dispatch_holdings import _edit, _held_by, _orphaned_attempt

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="reads the real POSIX ps")

PLATFORM_BINARIES = [
    ["/bin/sleep", "60"],
    ["/bin/bash", "-c", "sleep 60; true"],
    ["/usr/bin/perl", "-e", "sleep 60"],
]
SPAWN = "import subprocess, sys, time; subprocess.Popen(sys.argv[1:]); time.sleep(60)"
ORPHAN = "import subprocess as s; print(s.Popen(['/bin/sleep', '60'], stdout=s.DEVNULL).pid, flush=True)"


def test_p4_real_posix_inventory_is_trusted_on_a_quiet_machine(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(procs, "snapshot", REAL_SNAPSHOT)
    assert allocation.owned_pids(tmp_path, "001-mac-01") == ([], True)


def test_p4_crashed_supervisor_holdings_are_freed_by_collect_with_the_real_inventory(
    droot: Path, monkeypatch
):
    d = _orphaned_attempt(droot, monkeypatch)
    monkeypatch.setattr(procs, "snapshot", REAL_SNAPSHOT)
    [row] = dispatch.collect(droot, dispatch_id=d["id"])
    assert row["state"] == "failed"
    assert "holdings_held" not in dispatch.load(droot, d["id"])
    assert _held_by(droot, f"dispatch:{d['id']}") == []
    settle(droot, d)
    assert _new_fast(droot, watch=False)["slot"] == 0


def _parent_of(argv: list[str], env: dict[str, str]) -> tuple[subprocess.Popen, int]:
    """A python parent in its own group that starts `argv` and waits; returns it and the child pid."""
    parent = subprocess.Popen([sys.executable, "-c", SPAWN, *argv], env=env, start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        children = [p.pid for p in REAL_SNAPSHOT() if p.ppid == parent.pid]
        if children:
            return parent, children[0]
        time.sleep(0.05)
    os.killpg(parent.pid, signal.SIGKILL)
    raise AssertionError(f"{argv[0]} never started")


@pytest.mark.parametrize("argv", PLATFORM_BINARIES, ids=lambda argv: Path(argv[0]).name)
def test_p4_platform_binary_descendant_is_not_invisible_to_the_inventory(droot: Path, monkeypatch, argv):
    if not Path(argv[0]).exists():
        pytest.skip(f"{argv[0]} is not on this machine")
    d = _orphaned_attempt(droot, monkeypatch)
    monkeypatch.setattr(procs, "snapshot", REAL_SNAPSHOT)
    tagged = {**os.environ, procs.TAG_DISPATCH: d["id"], procs.TAG_ROOT: str(droot)}
    untagged = {k: v for k, v in tagged.items() if k not in (procs.TAG_DISPATCH, procs.TAG_ROOT)}
    started: list[subprocess.Popen] = []
    try:
        worker, child = _parent_of(argv, tagged)
        started.append(worker)
        server, served = _parent_of(argv, untagged)
        started.append(server)
        pids, trusted = allocation.owned_pids(droot, d)
        assert child in pids or not trusted
        pids, trusted = allocation.owned_pids(droot, {**d, "claude_pid": server.pid})
        assert {server.pid, served} <= set(pids) or not trusted
        dispatch.collect(droot, dispatch_id=d["id"])
        dispatch.collect(droot)
        assert dispatch.load(droot, d["id"]).get("holdings_held")
        assert len(_held_by(droot, f"dispatch:{d['id']}")) == 2
    finally:
        for parent in started:
            os.killpg(parent.pid, signal.SIGKILL)
            parent.wait()


def _row(pid: int) -> procs.Proc | None:
    return next((p for p in REAL_SNAPSHOT() if p.pid == pid), None)


def test_p4_orphan_in_the_dead_worker_group_keeps_the_holdings(droot: Path, monkeypatch):
    d = _orphaned_attempt(droot, monkeypatch)
    monkeypatch.setattr(procs, "snapshot", REAL_SNAPSHOT)
    untagged = {k: v for k, v in os.environ.items() if k not in (procs.TAG_DISPATCH, procs.TAG_ROOT)}
    leader = subprocess.Popen(
        [sys.executable, "-c", ORPHAN],
        env=untagged,
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    orphan = int(leader.communicate(timeout=10)[0])
    try:
        deadline = time.monotonic() + 10
        while (row := _row(orphan)) and row.ppid == leader.pid and time.monotonic() < deadline:
            time.sleep(0.05)
        assert row and row.pgid == leader.pid and row.ppid != leader.pid
        d = _edit(droot, d["id"], claude_pid=leader.pid)
        pids, trusted = allocation.owned_pids(droot, d)
        assert orphan in pids and trusted
        holdings.release(droot, d)
        assert dispatch.load(droot, d["id"]).get("holdings_held")
        assert len(_held_by(droot, f"dispatch:{d['id']}")) == 2
    finally:
        os.kill(orphan, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while _row(orphan) and time.monotonic() < deadline:
        time.sleep(0.05)
    [row] = dispatch.collect(droot, dispatch_id=d["id"])
    assert row["state"] == "failed"
    assert "holdings_held" not in dispatch.load(droot, d["id"])
    assert _held_by(droot, f"dispatch:{d['id']}") == []
