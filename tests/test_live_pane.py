"""The live plugin's real renderer under Node: stable nodes, cell patches and deadlines."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli.vault.dashboard import render_dashboard
from live_pane_helpers import texts, view

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "harness/obsidian/plugins/agent-terminal-focus/main.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
def test_background_and_parked_managers_keep_identity_and_lease_actions(tmp_path: Path):
    # Plan §E2 ports the legacy assertions; LivePane labels background rows in one table.
    fork = {
        "session_id": "fork1234-5678",
        "name": "mgr-fork (bg)",
        "cost_usd": 0.3,
        "leases": ["vault:src"],
        "clear_leases": True,
    }
    tab = {"session_id": "tab", "name": "mgr-tab", "state": "idle", "parked": [fork]}
    snap = {"managers": [tab, {**fork, "session_id": "other", "name": "mgr-other", "background": True}]}
    nodes = view(tmp_path, "managers", 0, snap)
    shown = texts(nodes)
    assert "Background session" in shown and "mgr-other" in shown
    assert "parked in background: mgr-fork (bg) (fork1234) · $0.30" in shown
    assert "holds vault:src" in shown
    assert ["a", "Clear leases", "obsidian://agent-lease-clear?session=fork1234-5678"] in nodes
    assert ["a", "Clear leases", "obsidian://agent-lease-clear?session=tab"] not in nodes
    assert sum(n == ["span", "mgr-tab"] for n in nodes) == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the plugin")
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_scoped_editor_find_debounce_and_async_snapshots(platform: str):
    result = subprocess.run(
        ["node", str(ROOT / "tests/fixtures/probe_terminal_focus.js"), str(PLUGIN), platform],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "platform": platform,
        "untouched": 9,
        "debounceScans": 1,
        "cachedDeadlines": True,
        "asyncSnapshots": True,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the plugin")
def test_live_pane_patches_rows_and_keeps_activity_order():
    result = subprocess.run(
        ["node", str(ROOT / "tests/fixtures/probe_dashboard.js"), str(PLUGIN)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "polls": 3,
        "containerReplacements": 0,
        "rowReplacements": 0,
        "changedCellPatches": 1,
        "activityReorders": 0,
        "workers": 47,
        "deadlineChecks": 2,
        "grouping": {
            "features": 10,
            "tasks": 16,
            "rowCreations": 0,
            "rowRemovals": 0,
            "statusChangeRebuilds": 0,
        },
    }


def test_dashboard_embeds_live_pane_and_keeps_static_views(tmp_path: Path):
    text = render_dashboard(tmp_path)
    assert "```agent-live\n```" in text
    assert '/live"' not in text
    for name in ("journal", "log", "nav"):
        assert f'/{name}"' in text
    for name in ("features", "tasks"):
        assert f'/{name}"' not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the plugin")
def test_stale_tabs_wait_for_consecutive_reads():
    result = subprocess.run(
        ["node", str(ROOT / "tests/fixtures/probe_stale_tabs.js"), str(PLUGIN)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "alternatingPaints": 1,
        "confirmedStalePaints": 1,
        "recoveryPaints": 1,
        "ownerQuestionExtraPaints": 0,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the compatibility views")
def test_feature_and_task_views_use_canonical_snapshot():
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/fixtures/probe_canonical_views.js"),
            str(ROOT / "harness/obsidian/views/features.js"),
            str(ROOT / "harness/obsidian/views/tasks.js"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "canonicalCount": 2,
        "canonicalTaskNumbers": [2, 5],
        "dataviewReads": 0,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the worker view")
def test_worker_tail_keeps_rows_during_polls():
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/fixtures/probe_worker.js"),
            str(ROOT / "harness/obsidian/views/worker.js"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"polls": 3, "containerClears": 0, "retainedRows": 1, "addedRows": 1}
