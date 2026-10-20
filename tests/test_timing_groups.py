"""The timing groups bind (tests/timing_groups.py; Codex's phase 3-5 review: the hook ran after
xdist read the group markers, so no group was ever kept on one worker)."""

from __future__ import annotations

from pathlib import Path

import timing_groups


def test_the_hook_runs_before_xdist_reads_the_groups():
    opts: dict[str, object] = getattr(timing_groups.pytest_collection_modifyitems, "pytest_impl", {})
    assert opts.get("tryfirst") is True


def test_every_timing_file_exists():
    tests = Path(__file__).resolve().parent
    assert sorted(f for f in timing_groups.TIMING_FILES if not (tests / f).is_file()) == []
