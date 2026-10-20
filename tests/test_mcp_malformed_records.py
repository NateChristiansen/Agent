"""MCP records of the wrong shape read as empty or are skipped; they do not raise."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_cli.core import AgentError
from agent_cli.mcp import fresh, staleness


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _stamp(root: Path, record: dict[str, object]) -> Path:
    path = root / staleness.STAMP_DIR / "server-1.json"
    _write(path, record)
    return path


def _newer_source(root: Path) -> None:
    src = root / "src" / "agent_cli" / "thing.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x = 1\n", encoding="utf-8")
    os.utime(src, (2_000.0, 2_000.0))


def test_a_stamp_whose_code_mtime_is_not_a_number_reads_as_stale(tmp_root: Path):
    _newer_source(tmp_root)
    _stamp(tmp_root, {"pid": os.getpid(), "code_mtime": "junk"})
    assert [s["pid"] for s in staleness.stale_servers(tmp_root)] == [os.getpid()]


def test_a_stamp_whose_pid_is_not_a_number_is_swept(tmp_root: Path):
    _newer_source(tmp_root)
    path = _stamp(tmp_root, {"pid": str(os.getpid()), "code_mtime": 1_000.0})
    assert staleness.stale_servers(tmp_root) == []
    assert not path.exists()


def test_a_fresh_reply_whose_ok_is_not_an_object_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    done = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"ok": "x"}\n', stderr="")
    monkeypatch.setattr(fresh.subprocess, "run", lambda *a, **kw: done)
    monkeypatch.setattr(fresh, "_session_env", lambda root: {})
    with pytest.raises(AgentError, match="not an object"):
        fresh.run_fresh(tmp_path, "manager", "feature_list", {})
