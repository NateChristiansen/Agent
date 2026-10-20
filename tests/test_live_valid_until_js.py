"""Presentation contract on the Dashboard: past
`valid_until`, every section reads unknown or stale, never the last state it was told."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from live_pane_helpers import texts, view

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node runs the Dashboard view script")

SNAP: dict[str, Any] = {
    "rendered_at": "2000-01-01T00:00:00Z",
    "valid_until": "2000-01-01T00:00:15Z",
    "managers": [
        {
            "session_id": "probe",
            "name": "probe",
            "state": "idle",
            "activity": "idle",
            "background": False,
            "running": [],
            "waiting": [],
            "leases": [],
            "parked": [],
        }
    ],
    "workers": [
        {
            "id": "001-mac-01",
            "state": "running",
            "repo": "alpha",
            "goal": "g",
            "open": "x",
            "started": "2000-01-01T00:00:00Z",
            "last_activity": "2000-01-01T00:00:00Z",
        }
    ],
    "needs_you": [
        {"kind": "question", "name": "mgr-1", "reason": "question", "since": "2000-01-01T00:00:00Z"}
    ],
}


@pytest.mark.parametrize("section", ["managers", "workers", "needs_you"])
def test_an_expired_snapshot_reads_unknown_and_warns_in_every_section(tmp_path: Path, section: str):
    nodes = view(tmp_path, section, 3600, SNAP)
    shown = texts(nodes)
    assert "⚠ stale snapshot" in shown
    assert "unknown" in shown
    if section == "managers":
        assert ["span", "idle"] not in nodes


@pytest.mark.parametrize("section", ["managers", "workers", "needs_you"])
def test_a_current_snapshot_is_drawn_as_it_is(tmp_path: Path, section: str):
    nodes = view(tmp_path, section, 0, SNAP)
    shown = texts(nodes)
    assert "stale" not in shown and "unknown" not in shown
    if section == "managers":
        assert ["span", "idle"] in nodes


def test_one_expired_item_reads_unknown_alone(tmp_path: Path):
    snap = {**SNAP, "valid_until": None}
    snap["needs_you"] = [{**SNAP["needs_you"][0], "valid_until": "2000-01-01T00:00:15Z"}]
    shown = texts(view(tmp_path, "needs_you", 3600, snap))
    assert "unknown (stale): " in shown and "⚠ stale snapshot" not in shown
