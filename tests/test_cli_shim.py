import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli.leases import LEASE_STATUSES

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "bin" / ("agent.cmd" if sys.platform == "win32" else "agent")


def test_shim_runs_from_another_cwd(tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ROOT"}
    proc = subprocess.run([str(SHIM), "--version"], cwd=tmp_path, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("agent ")


def test_shim_preserves_caller_cwd_but_finds_root(tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ROOT"}
    proc = subprocess.run(
        [str(SHIM), "--json", "scratch", "path"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # the shim found the root from its own location, not the caller's cwd. This once listed the
    # registry's first repo, which read the owner's `agent.yaml` and broke when it was empty.
    assert Path(json.loads(proc.stdout)["path"]) == ROOT / ".agent" / "scratch"


@pytest.mark.live_config
def test_shim_session_list(tmp_path: Path):
    """`session list` (item 2) as a CLI subcommand, wired through the real shim. Read-only:
    never takes or waits on a lease here (harness/sessions.md: real-vault leases are the
    real vault's, not a test's to touch)."""
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ROOT"}
    proc = subprocess.run(
        [str(SHIM), "--json", "session", "list"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    records = json.loads(proc.stdout)
    assert isinstance(records, list)
    for record in records:
        assert set(record) >= {"session_id", "name", "feature", "pid", "state", "started_at"}
        assert record["state"] in ("LIVE", "DEAD")


@pytest.mark.live_config
def test_shim_lease_list(tmp_path: Path):
    """`lease list` (item 3), read-only — see `test_shim_session_list`."""
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ROOT"}
    proc = subprocess.run(
        [str(SHIM), "--json", "lease", "list"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    leases = json.loads(proc.stdout)
    assert isinstance(leases, list)
    for lease in leases:
        # the module's own list: `resting` passed this test by luck for five commits
        assert lease["status"] in LEASE_STATUSES


def test_shim_status_line(tmp_root: Path):
    """`status line` through the real shim.

    Empty stdin (piped, so not a TTY) on an isolated `--root` with nothing recorded gives
    empty output and exit 0."""
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ROOT"}
    proc = subprocess.run(
        [str(SHIM), "--root", str(tmp_root), "status", "line"],
        cwd=tmp_root,
        input="",
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
