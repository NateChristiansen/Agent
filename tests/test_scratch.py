"""`.agent/scratch/`: created on demand."""

from __future__ import annotations

from pathlib import Path

from agent_cli import scratch


def test_ensure_creates_subdirs(tmp_root: Path):
    base = scratch.ensure(tmp_root)
    assert base == tmp_root / ".agent" / "scratch"
    for name in scratch.SUBDIRS:
        assert (base / name).is_dir()
