"""The live plugin's real renderer under Node: stable nodes, cell patches and deadlines."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import core
from agent_cli.vault import live
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


def _stop(root: Path, monkeypatch, *, outcomes: dict | None = None, minutes_ago: int = 0, **events: dict):
    """One Stop's capture on this machine, written where `live_health` reads it."""
    from datetime import timedelta

    from agent_cli import config_file, notes_sync, tab_plugin, telemetry
    from agent_cli.telemetry.dashboard import capture

    (root / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    monkeypatch.setattr(config_file, "notes_sync_mode", lambda r: "on")
    synced = {"at": core.iso(core.now()), "status": "current", "seq": 2}
    monkeypatch.setattr(
        notes_sync, "last_outcomes", lambda r: outcomes or {"last_attempt": synced, "last_ok": synced}
    )
    moment = core.now() - timedelta(minutes=minutes_ago)
    rec = telemetry.process_recorder()
    sink = telemetry.Sink.for_main(root)
    assert rec.record(sink, "activity.transition", ts=moment - timedelta(seconds=30), data={"to": "working"})
    for kind, fields in events.items():
        assert rec.record(sink, kind.replace("_", "."), ts=moment - timedelta(seconds=20), **fields)
    beat = root / tab_plugin.HEARTBEAT
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"at": moment.timestamp(), "at_iso": core.iso(moment)}), encoding="utf-8")
    record = capture(root, moment.astimezone().date(), {"machine": "mac", "status": "partial"}, now=moment)
    path = core.agent_path(root, "usage", "telemetry", f"{moment.astimezone().date()}.mac.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _queue(root: Path, count: int) -> None:
    from agent_cli import lessons

    for n in range(count):
        lessons.add(root, title=f"finding {n}", source="none", evidence="ordinary evidence", home="")


def _health(nodes: list[list[str]]) -> list[str]:
    shown = [str(n[1]) for n in nodes]
    return shown[next(i for i, t in enumerate(shown) if t.startswith("Health")) :]


HOOKS_MEANING = (
    "Whether harness hooks ran without raising. A hook that raised may have skipped its work "
    "(tab icon, usage, notes sync)."
)
DISPLAY_MEANING = "Whether manager tab icons, generated views and this pane render."


def _windows(root: Path, **health: dict) -> None:
    """This machine's fresh record published again as windows, with `health` merged in."""
    from agent_cli.telemetry.summary import summary_rel

    remote = json.loads(
        core.agent_path(root, "usage", "telemetry", f"{core.today()}.mac.json").read_text(encoding="utf-8")
    )
    remote["machine"] = "windows"
    for key, fields in health.items():
        remote["health"][key].update(fields)
    path = root / summary_rel(core.today(), "windows")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(remote), encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
def test_a_normal_day_is_one_line_with_the_lesson_count(tmp_root: Path, monkeypatch):
    _stop(tmp_root, monkeypatch)
    _queue(tmp_root, 6)
    shown = _health(view(tmp_root, None, 0, live.build(tmp_root)))
    assert shown[0] == "Health: all clear"
    assert "4 ok: Notes sync, Hooks, Vault display, lessons: 6 open" in shown
    assert "ok · no errors today" in shown and "ok · lessons: 6 open" in shown
    assert shown[shown.index("ok · no errors today") + 1] == HOOKS_MEANING
    assert shown[shown.index("ok · tabs and views painting") + 1] == DISPLAY_MEANING
    assert not any(t.startswith("→") for t in shown)
    _windows(tmp_root)
    both = _health(view(tmp_root, None, 0, live.build(tmp_root)))
    assert both[0] == "Health: all clear"
    assert "4 ok: Notes sync, Hooks, Vault display, lessons: 6 open" in both, "each signal named once"
    assert "Hooks · windows" in both and "Hooks · mac" in both


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
def test_a_signal_failing_on_one_machine_is_never_named_ok_without_its_machine(tmp_root: Path, monkeypatch):
    _stop(tmp_root, monkeypatch)
    _queue(tmp_root, 6)
    _windows(
        tmp_root,
        notes_sync={
            "error_current": True,
            "last_error_status": "conflict",
            "last_error_at": core.iso(core.now()),
        },
        hook_errors={"count": 2, "last": {"hook": "activity", "at": core.iso(core.now())}},
    )
    shown = _health(view(tmp_root, None, 0, live.build(tmp_root)))
    assert shown[0] == "Health: 2 need action"
    assert "Notes sync · windows" in shown and "Hooks · windows" in shown
    assert "4 ok: Notes sync · mac, Hooks · mac, Vault display, lessons: 6 open" in shown


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
def test_a_signal_unknown_on_one_machine_is_never_named_ok_without_its_machine(tmp_root: Path, monkeypatch):
    _stop(tmp_root, monkeypatch)
    _queue(tmp_root, 6)
    _windows(tmp_root, coverage={"events": False})
    shown = _health(view(tmp_root, None, 0, live.build(tmp_root)))
    assert shown[0] == "Health: 2 unknown"
    hooks = shown.index(
        "unknown · no errors counted, but telemetry incomplete (event log missing or unreadable)"
    )
    assert shown[hooks + 1] == HOOKS_MEANING
    assert "4 ok: Notes sync, Hooks · mac, Vault display · mac, lessons: 6 open" in shown


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
@pytest.mark.parametrize(
    ("count", "heading", "line"),
    [
        (9, "Health: 1 needs action", "ok · lessons: 9 open"),
        (10, "Health: 2 need action", "needs action · lessons: 10 open — review needed"),
    ],
)
def test_action_and_unknown_rows_say_what_they_mean_and_what_to_do(
    tmp_root: Path, monkeypatch, count: int, heading: str, line: str
):
    _stop(tmp_root, monkeypatch, hook_error={"data": {"hook": "activity", "reason": "OSError"}})
    _queue(tmp_root, count)
    shown = _health(view(tmp_root, None, 0, live.build(tmp_root)))
    assert shown[0] == heading
    hooks = next(
        t for t in shown if t.startswith("needs action · 1 error today · last: activity (OSError), ")
    )
    after = shown[shown.index(hooks) + 1 :]
    assert after[0] == HOOKS_MEANING
    assert after[1] == '→ Ask the manager: "show today\'s hook errors and what failed".'
    assert line in shown
    if count == 10:
        assert "→ Run /lesson review in a manager." in shown
        assert "3 ok: Notes sync, Vault display" not in shown
        assert "2 ok: Notes sync, Vault display" in shown
    else:
        assert "3 ok: Notes sync, Vault display, lessons: 9 open" in shown


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
def test_missing_unreadable_and_expired_sources_never_look_healthy(tmp_root: Path, monkeypatch):
    from agent_cli import lessons

    (tmp_root / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    (tmp_root / lessons.QUEUE).mkdir(parents=True)
    shown = _health(view(tmp_root, None, 0, live.build(tmp_root)))
    assert shown[0] == "Health: 2 unknown"
    assert "unknown · no report today" in shown
    assert "unknown · lessons: unknown — queue unreadable" in shown
    no_block = _health(view(tmp_root, None, 0, {"observed_at": "2026-10-15T00:00:00Z"}))
    assert no_block[0] == "Health: 1 unknown"
    assert "unknown · not reported in this snapshot" in no_block


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
def test_this_machines_expired_rows_each_read_unknown_by_the_clock(tmp_root: Path, monkeypatch):
    outcomes = {
        "last_attempt": {"at": core.iso(core.now()), "status": "conflict", "seq": 3},
        "last_ok": {"at": core.iso(core.now()), "status": "current", "seq": 2},
        "last_error": {
            "at": core.iso(core.now()),
            "status": "conflict",
            "detail": "rebase conflict",
            "seq": 3,
        },
    }
    _stop(tmp_root, monkeypatch, outcomes=outcomes, minutes_ago=5)
    snap = live.build(tmp_root)
    fresh = _health(view(tmp_root, None, 0, snap))
    assert fresh[0] == "Health: 1 needs action"
    assert not any(t.startswith("unknown · no report") for t in fresh)
    later = _health(view(tmp_root, None, 11 * 60, snap))
    assert later[0] == "Health: 3 unknown", "observed 5 minutes before the build, so 16 minutes old"
    assert any(t.startswith("unknown · last report: conflict since ") for t in later)
    assert any(
        t.startswith("→ Open a manager on mac to refresh this report; if the problem remains:") for t in later
    )
    hooks = later.index("unknown · last report: no errors today")
    assert later[hooks + 1] == " · observed 16m ago"
    assert "→ The next turn end in a manager on this machine reports it again." in later
    assert "unknown · last report: tabs and views painting" in later
    assert not any(t.startswith("unknown · no report") for t in later), "no fold for this machine"
    assert "1 ok: lessons: 0 open" in later


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes LivePane")
def test_a_stale_remote_report_is_one_line_plus_its_problem_history(tmp_root: Path, monkeypatch):
    from datetime import timedelta

    from agent_cli.telemetry.summary import summary_rel

    _stop(tmp_root, monkeypatch)
    remote = json.loads(
        core.agent_path(tmp_root, "usage", "telemetry", f"{core.today()}.mac.json").read_text(
            encoding="utf-8"
        )
    )
    remote["machine"] = "windows"
    remote["updated"] = core.iso(core.now() - timedelta(hours=3))
    remote["health"]["vault_display"]["render_failures"] = 2
    remote["health"]["coverage"]["events"] = False
    path = tmp_root / summary_rel(core.today(), "windows")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(remote), encoding="utf-8")
    shown = _health(view(tmp_root, None, 0, live.build(tmp_root)))
    assert shown[0] == "Health: 3 unknown"
    assert shown[shown.index("unknown · no report") + 1 : shown.index("unknown · no report") + 3] == [
        " for 3h",
        " · last report had 2 problems",
    ]
    assert "unknown · last report: 2 render failures today" in shown
    reason = shown.index(
        "unknown · last report: no errors counted, but telemetry incomplete (event log missing or unreadable)"
    )
    assert shown[reason + 2].startswith(
        "→ Open a manager on windows to refresh this report; if the problem remains: "
    )
    assert not any("Notes sync · windows" in t for t in shown)
    assert "4 ok: Notes sync, Hooks · mac, Vault display · mac, lessons: 0 open" in shown


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
