"""Native Windows ownership snapshot and exact-process reaping."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent_cli import procs, win_snapshot

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process inventory tests")
PYTHON = getattr(sys, "_base_executable", sys.executable)


def _child(env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)"], env=env)


def test_snapshot_tags_and_reap_spare_unrelated(tmp_root: Path):
    token = f"win-test-{uuid.uuid4().hex}"
    owned_env = dict(os.environ)
    owned_env[procs.TAG_DISPATCH] = token
    unrelated_env = dict(os.environ)
    unrelated_env.pop(procs.TAG_DISPATCH, None)
    unrelated_env.pop(procs.TAG_SESSION, None)
    owned, unrelated = _child(owned_env), _child(unrelated_env)
    try:
        selected = procs.select(win_snapshot.snapshot(), dispatch=token)
        assert [p.pid for p in selected] == [owned.pid]
        assert selected[0].started
        result = procs.reap(tmp_root, selected)
        owned.wait(timeout=5)
        assert result["terminated"] == [owned.pid]
        assert unrelated.poll() is None
    finally:
        for child in (owned, unrelated):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_reap_rejects_stale_creation_time(tmp_root: Path):
    child = _child(dict(os.environ))
    try:
        target = procs.Proc(child.pid, os.getpid(), child.pid, "0", "child", started="stale")
        assert procs.reap(tmp_root, [target])["terminated"] == []
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)
