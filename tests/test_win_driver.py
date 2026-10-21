"""Windows dispatch cancellation must end only the tagged worker tree."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from agent_cli import core, procs
from agent_cli.dispatch import driver

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows dispatch tree tests")
PYTHON = getattr(sys, "_base_executable", sys.executable)


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not core.is_alive(pid):
            return True
        time.sleep(0.05)
    return not core.is_alive(pid)


def test_cancel_checks_dispatch_tag_and_ends_grandchild_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli import win_snapshot, win_tree

    token = f"win-driver-{uuid.uuid4().hex}"
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([{PYTHON!r},'-c','import time; time.sleep(61)']); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    owned_env = dict(os.environ)
    owned_env[procs.TAG_DISPATCH] = token
    owned_env[procs.TAG_ROOT] = str(tmp_path)  # kill_group selects by dispatch id within this root
    unrelated_env = dict(os.environ)
    unrelated_env.pop(procs.TAG_DISPATCH, None)
    parent = subprocess.Popen([PYTHON, "-c", code], env=owned_env)
    unrelated = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)"], env=unrelated_env)
    grandchild = None
    try:
        deadline = time.monotonic() + 5
        # write_text creates the file before it writes the pid
        while not (pidfile.exists() and pidfile.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        grandchild = int(pidfile.read_text())
        monkeypatch.setattr(procs, "snapshot", win_snapshot.snapshot)
        driver.kill_group(parent.pid, root=tmp_path, dispatch_id="wrong-dispatch")
        assert parent.poll() is None and core.is_alive(grandchild)
        target = next(p for p in win_snapshot.snapshot() if p.pid == parent.pid)
        monkeypatch.setattr(procs, "snapshot", lambda: [replace(target, started="stale")])
        driver.kill_group(parent.pid, root=tmp_path, dispatch_id=token)
        assert parent.poll() is None and core.is_alive(grandchild)
        monkeypatch.setattr(procs, "snapshot", win_snapshot.snapshot)
        driver.kill_group(parent.pid, root=tmp_path, dispatch_id=token)
        parent.wait(timeout=5)
        assert _gone(grandchild)
        assert unrelated.poll() is None
    finally:
        if parent.poll() is None:
            win_tree.kill_tree(parent.pid)
        if grandchild is not None and core.is_alive(grandchild):
            win_tree.kill_tree(grandchild)
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_cancel_reaps_tagged_grandchild_after_leader_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli import win_snapshot, win_tree
    from agent_cli.winproc import pid_started  # pyright: ignore[reportAttributeAccessIssue] — win32-only

    token = f"win-driver-exited-{uuid.uuid4().hex}"
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import pathlib,subprocess,sys; "
        f"p=subprocess.Popen([{PYTHON!r},'-c','import time; time.sleep(61)']); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))"
    )
    owned_env = dict(os.environ)
    owned_env[procs.TAG_DISPATCH] = token
    owned_env[procs.TAG_ROOT] = str(tmp_path)  # kill_group selects by dispatch id within this root
    unrelated_env = dict(os.environ)
    unrelated_env.pop(procs.TAG_DISPATCH, None)
    parent = subprocess.Popen([PYTHON, "-c", code], env=owned_env)
    unrelated = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)"], env=unrelated_env)
    grandchild = None
    try:
        parent.wait(timeout=5)
        grandchild = int(pidfile.read_text())
        assert core.is_alive(grandchild)
        observed = next((p for p in win_snapshot.snapshot() if p.pid == grandchild), None)
        assert observed is not None and observed.dispatch == token and observed.started, (
            f"grandchild={grandchild}, snapshot={observed}, started={pid_started(grandchild)}"
        )

        monkeypatch.setattr(procs, "snapshot", win_snapshot.snapshot)
        reports: list[dict] = []
        selections: list[tuple[str, list[int]]] = []
        snapshots: list[list[tuple[int, str, str]]] = []
        real_reap = procs.reap
        real_select = procs.select
        real_snapshot = procs.snapshot

        def record_snapshot():
            rows = real_snapshot()
            snapshots.append([(p.pid, p.dispatch, p.started) for p in rows if p.pid == grandchild])
            return rows

        def record_select(rows: list[procs.Proc], *, session="", dispatch="", root=None, escaped_only=False):
            selected = real_select(
                rows, session=session, dispatch=dispatch, root=root, escaped_only=escaped_only
            )
            selections.append((dispatch, [p.pid for p in selected]))
            return selected

        def record_reap(root: Path, targets: list[procs.Proc]):
            report = real_reap(root, targets)
            reports.append(report)
            return report

        monkeypatch.setattr(procs, "reap", record_reap)
        monkeypatch.setattr(procs, "snapshot", record_snapshot)
        monkeypatch.setattr(procs, "select", record_select)

        driver.kill_group(parent.pid, root=tmp_path, dispatch_id=token)

        assert _gone(grandchild), (
            f"grandchild={grandchild}, before={observed}, snapshots={snapshots}, "
            f"selections={selections}, reports={reports}, "
            f"driver_platform={driver.sys.platform}, after={pid_started(grandchild)}"
        )
        assert unrelated.poll() is None
    finally:
        if parent.poll() is None:
            win_tree.kill_tree(parent.pid)
        if grandchild is not None and core.is_alive(grandchild):
            win_tree.kill_tree(grandchild)
        unrelated.kill()
        unrelated.wait(timeout=5)
