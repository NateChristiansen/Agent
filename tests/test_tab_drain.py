"""Concurrent tab state reaches Obsidian through one bounded writer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import obsidian_tabs, sessions, tab_drain
from hooks_test_helpers import enable_tab_drain as _enable


def test_four_managers_128_events_need_one_eval(tmp_root: Path, monkeypatch):
    """The prior per-manager throttle could still launch 16 evals in this four-turn replay."""
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)

    class Process:
        pid = 99999

    launches: list[int] = []
    monkeypatch.setattr(tab_drain.subprocess, "Popen", lambda *a, **kw: launches.append(1) or Process())
    original_alive = tab_drain.core.is_alive
    monkeypatch.setattr(tab_drain.core, "is_alive", lambda pid: pid == 99999 or original_alive(pid))
    names = ["mgr-034-claude", "mgr-034-codex-1", "mgr-035-claude", "mgr-036-codex-2"]
    for i, name in enumerate(names):
        sid = f"session-{i}"
        sessions.set_record(tmp_root, sid, name=name)
        for event in range(32):
            icon = ("✅", "⚙️", "✋", "⏳")[event // 8]
            tab_drain.request(tmp_root, "manager", sid, name, f"{icon} {name}")
    assert len(launches) == 1
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    calls: list[list[str]] = []

    def evaluate(entries):
        calls.append([e["key"] for e in entries])
        return set(calls[-1])

    monkeypatch.setattr(tab_drain, "_eval", evaluate)
    tab_drain.run(tmp_root)
    assert len(calls) == 1 and len(calls[0]) == 4
    painted = tab_drain._state(tmp_root)["painted"]
    assert all(painted[f"manager-session-{i}"]["label"] == f"⏳ {name}" for i, name in enumerate(names))
    metrics = json.loads(
        (tmp_root / tab_drain.TABS_DIR / "metrics.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert {row["key"] for row in metrics["painted"]} == set(calls[0])
    assert all(row["label"].startswith("⏳ ") for row in metrics["painted"])


def test_stop_written_during_eval_gets_its_own_paint(tmp_root: Path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sid = "codex-1"
    name = "mgr-036-codex-1"
    sessions.set_record(tmp_root, sid, name=name)
    tab_drain.request(tmp_root, "manager", sid, name, f"⚙️ {name}")
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    labels: list[str] = []

    def evaluate(entries):
        labels.append(entries[0]["label"])
        if len(labels) == 1:
            tab_drain.request(tmp_root, "manager", sid, name, f"✅ {name}")
        return {entries[0]["key"]}

    monkeypatch.setattr(tab_drain, "_eval", evaluate)
    tab_drain.run(tmp_root)
    assert labels == [f"⚙️ {name}", f"✅ {name}"]
    assert (sessions.get(tmp_root, sid) or {})["tab_label"] == f"✅ {name}"


def test_closed_tab_stops_after_retry_cap(tmp_root: Path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sid = "claude-1"
    sessions.set_record(tmp_root, sid, name="mgr-036-x")
    tab_drain.request(tmp_root, "manager", sid, "mgr-036-x", "✅ mgr-036-x")
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    calls: list[int] = []
    monkeypatch.setattr(tab_drain, "_eval", lambda entries: calls.append(1) or set())
    tab_drain.run(tmp_root)
    assert len(calls) == tab_drain.RETRY_LIMIT
    assert tab_drain._state(tmp_root)["runner"] is None
    assert tab_drain.request(tmp_root, "manager", sid, "mgr-036-x", "✅ mgr-036-x")["reason"] == (
        "unpaintable"
    )


def test_an_obsidian_that_stays_down_stops_the_drainer_without_spending_retries(tmp_root: Path, monkeypatch):
    """A failed eval is not a missing tab (session ae3b, 2026-10-04): the drainer gives up after
    `EVAL_FAILURE_LIMIT` in a row, and the next request starts another for the same label."""
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sid = "claude-1"
    sessions.set_record(tmp_root, sid, name="mgr-036-x")
    tab_drain.request(tmp_root, "manager", sid, "mgr-036-x", "✋ mgr-036-x")
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    calls: list[int] = []
    monkeypatch.setattr(tab_drain, "_eval", lambda entries: calls.append(1) or None)
    tab_drain.run(tmp_root)
    assert len(calls) == tab_drain.EVAL_FAILURE_LIMIT
    assert tab_drain._state(tmp_root)["runner"] is None
    assert tab_drain.request(tmp_root, "manager", sid, "mgr-036-x", "✋ mgr-036-x") == {
        "started": True,
        "queued": True,
    }


def test_a_hand_refused_under_the_old_retry_rules_is_repainted(tmp_root: Path, monkeypatch):
    """Session ae3b's live state, 2026-10-04: its ✋ was counted unpaintable under match version
    3, when failed evals spent retries. The version bump gives that label fresh retries."""
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sid, name = "ae3b0000", "008 harness-bug-bash · cl ae3b"
    label = f"✋ {name}"
    sessions.set_record(tmp_root, sid, name="mgr-008-harness-bug-bash")
    key = f"manager-{sid}"
    path = tmp_root / tab_drain.TABS_DIR / f"{key}.json"
    path.parent.mkdir(parents=True)
    old = {"key": key, "kind": "manager", "identity": sid, "match": name, "token": " · cl ae3b"}
    old |= {"generated_name": "mgr-008-harness-bug-bash", "label": label, "match_version": 3}
    path.write_text(json.dumps({**old, "generated": True, "reset": False}), encoding="utf-8")
    tab_drain._save_state(
        tmp_root,
        {
            "painted": {key: {"label": None, "name": name}},
            "attempts": {key: {"label": label, "match_version": 3, "count": tab_drain.RETRY_LIMIT}},
        },
    )
    result = tab_drain.request(
        tmp_root, "manager", sid, name, label, token=" · cl ae3b", generated_name="mgr-008-harness-bug-bash"
    )
    assert result == {"started": True, "queued": True}
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    shown: list[str] = []
    monkeypatch.setattr(
        tab_drain, "_eval", lambda entries: shown.extend(e["label"] for e in entries) or {key}
    )
    tab_drain.run(tmp_root)
    assert shown == [label]
    assert (sessions.get(tmp_root, sid) or {})["tab_label"] == label


def test_a_paint_reported_missing_does_not_vouch_for_the_old_label(tmp_root: Path, monkeypatch):
    """Replayed from a live check (`.agent/tabs/metrics.jsonl`): ⏳ painted at the end of the turn, a subagent's hand-back turn
    asked for ⚙️, and that eval reported no tab found, yet the owner saw ⚙️. The next Stop asked
    for ⏳ again, which the drainer skipped as already painted, so the tab kept ⚙️ over an idle
    manager until the background command finished."""
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sid, name = "a3ae0000", "new · cl a3ae"
    sessions.set_record(tmp_root, sid, name=name)
    shown: list[str] = []

    def evaluate(entries):
        shown.append(entries[0]["label"])  # the tab now reads this, whatever the eval returns
        return set() if entries[0]["label"].startswith("⚙️") else {entries[0]["key"]}

    monkeypatch.setattr(tab_drain, "_eval", evaluate)

    def paint(label: str) -> None:
        tab_drain.request(tmp_root, "manager", sid, name, label)
        state = tab_drain._state(tmp_root)
        state["runner"] = os.getpid()
        tab_drain._save_state(tmp_root, state)
        tab_drain.run(tmp_root)

    paint(f"⏳ {name}")  # Stop: a subagent and `sleep 20` still running
    paint(f"⚙️ {name}")  # UserPromptSubmit: the subagent's hand-back starts a turn
    paint(f"⏳ {name}")  # Stop: `sleep 20` still running
    assert shown[-1] == f"⏳ {name}"


def test_new_matcher_retries_a_prior_unpaintable_name(tmp_root: Path, monkeypatch):
    _enable(monkeypatch)
    sessions.set_record(tmp_root, "s1", name="mgr-036-new")
    path = tmp_root / tab_drain.TABS_DIR / "manager-s1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"key":"manager-s1","kind":"manager","identity":"s1",'
        '"match":"mgr-035-old","label":"✅ mgr-036-new","match_version":1}',
        encoding="utf-8",
    )
    tab_drain._save_state(
        tmp_root,
        {"attempts": {"manager-s1": {"label": "✅ mgr-036-new", "match_version": 1, "count": 3}}},
    )
    assert tab_drain.request(tmp_root, "manager", "s1", "mgr-035-old", "✅ mgr-036-new") == {
        "started": True,
        "queued": True,
    }
    assert tab_drain.read_entry(path)["match_version"] == tab_drain.MATCH_VERSION


def test_ended_session_is_pruned_before_an_eval(tmp_root: Path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain.core, "is_alive", lambda pid: False)
    sessions.set_record(tmp_root, "s1", pid=987654, name="mgr-036-ended")
    tab_drain.request(tmp_root, "manager", "s1", "mgr-036-ended", "✅ mgr-036-ended")
    state = tab_drain._state(tmp_root)
    pending = tab_drain._pending(tmp_root, state)
    assert pending == []
    assert not (tmp_root / tab_drain.TABS_DIR / "manager-s1.json").exists()


def test_excess_global_eval_rate_trips_the_kill_switch(tmp_root: Path, monkeypatch):
    import time

    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sessions.set_record(tmp_root, "s1", name="mgr-036-x")
    tab_drain.request(tmp_root, "manager", "s1", "mgr-036-x", "✅ mgr-036-x")
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    state["eval_times"] = [time.time()] * tab_drain.MAX_EVALS_PER_MINUTE
    tab_drain._save_state(tmp_root, state)
    calls: list[int] = []
    monkeypatch.setattr(tab_drain, "_eval", lambda entries: calls.append(1) or set())
    tab_drain.run(tmp_root)
    assert calls == []
    assert (tmp_root / obsidian_tabs.TABS_OFF_FILE).exists()


def test_kill_switch_mid_eval_stops_without_recording_a_paint(tmp_root: Path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sessions.set_record(tmp_root, "s1", name="mgr-036-x")
    tab_drain.request(tmp_root, "manager", "s1", "mgr-036-x", "✅ mgr-036-x")
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)

    def evaluate(entries):
        (tmp_root / obsidian_tabs.TABS_OFF_FILE).touch()
        return {entries[0]["key"]}

    monkeypatch.setattr(tab_drain, "_eval", evaluate)
    tab_drain.run(tmp_root)
    assert not tab_drain._state(tmp_root).get("painted")


def test_worker_watch_uses_the_shared_gate(tmp_root: Path, monkeypatch):
    calls: list[tuple[str, str, str, bool]] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, identity, match, label, *, generated: (
            calls.append((kind, match, label, generated)) or {"started": True}
        ),
    )
    obsidian_tabs.relabel_worker(tmp_root, "001-01", "running")
    assert calls == [("watch", "001-01 watch", "◐ 001-01 watch", False)]


def test_ambiguous_name_does_not_paint_two_tabs():
    code = tab_drain._batch_code(
        [{"key": "manager-a", "match": "mgr-036-x", "label": "✅ mgr-036-x", "generated": True}]
    )
    assert "matches.length !== 1" in code
    assert "matches.length !== 1" in code
    assert "user.endsWith(e.token)" in code
    assert "eq(user, e.match)" in code
    assert "const matches = exact.length ? exact : token" in code


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to execute Obsidian eval JS")
def test_exact_name_wins_over_a_token_match():
    code = tab_drain._batch_code(
        [
            {
                "key": "manager-a",
                "match": "old-name",
                "token": " · cl 1b06",
                "generated_name": "mgr-new-name",
                "label": "✅ new-name · cl 1b06",
                "generated": True,
            }
        ]
    )
    setup = """
const leaves = [
  {view: {state: {userTitle: '⚙️ old-name'}}, getDisplayText() { return this.view.state.userTitle; }},
  {view: {state: {userTitle: '✅ other · cl 1b06'}}, getDisplayText() { return this.view.state.userTitle; }}
];
globalThis.app = {workspace: {getLeavesOfType: () => leaves}};
"""
    script = (
        setup
        + f"console.log(JSON.stringify([eval({json.dumps(code)}), leaves.map(l => l.view.state.userTitle)]));"
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert json.loads(result.stdout) == [
        "AGENT_TAB_RESULTS:manager-a",
        ["✅ new-name · cl 1b06", "✅ other · cl 1b06"],
    ]


def test_ambiguity_writes_one_diagnostic_marker(tmp_root: Path):
    entry = {
        "key": "manager-a",
        "identity": "a",
        "kind": "manager",
        "label": "✅ new · cl 1b06",
        "match_version": tab_drain.MATCH_VERSION,
    }
    state: dict = {}
    tab_drain._record(tmp_root, state, [entry], {"ambiguous.manager-a"})
    marker = tmp_root / tab_drain.TABS_DIR / "ambiguous-manager-a.json"
    first = marker.read_text(encoding="utf-8")
    tab_drain._record(tmp_root, state, [entry], {"ambiguous.manager-a"})
    assert marker.read_text(encoding="utf-8") == first
    assert state["attempts"]["manager-a"]["count"] == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to execute Obsidian eval JS")
def test_a_token_only_request_paints_only_the_tab_with_that_suffix():
    """#59's fallback has no name to match, so a tab named like the session but without its
    `· cx <id>` suffix is never painted (Codex review), and the suffixed one is."""
    entry = {
        "key": "manager-a",
        "match": "",
        "token": " · cx 0001",
        "generated_name": "",
        "label": "✅ 001 example · cx 0001",
        "generated": False,
    }
    setup = """
const leaves = [
  {view: {state: {userTitle: '✋ 001 example'}}, getDisplayText() { return this.view.state.userTitle; }},
  {view: {state: {userTitle: '✋ 001 example · cx 0001'}}, getDisplayText() { return this.view.state.userTitle; }}
];
globalThis.app = {workspace: {getLeavesOfType: () => leaves}};
"""
    code = tab_drain._batch_code([entry])
    script = (
        setup
        + f"console.log(JSON.stringify([eval({json.dumps(code)}), leaves.map(l => l.view.state.userTitle)]));"
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert json.loads(result.stdout) == [
        "AGENT_TAB_RESULTS:manager-a",
        ["✋ 001 example", "✅ 001 example · cx 0001"],
    ]
    none = subprocess.run(
        ["node", "-e", setup.replace(" · cx 0001", "") + f"console.log(eval({json.dumps(code)}));"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert none.stdout.strip() == "AGENT_TAB_RESULTS:"


def test_fallback_stale_hysteresis_and_owner_question(tmp_path: Path, monkeypatch):
    from agent_cli import tab_drain

    clock = [100.0]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    entry = {"key": "ordinary", "label": "✋ ordinary"}
    tab = {"label": entry["label"], "stale_label": "✋? ordinary", "stale_reads": 3}
    snapshot = {"valid_until": 99.0, "tabs": {"ordinary": tab}}
    for i in range(20):
        clock[0] += 2
        snapshot["valid_until"] = clock[0] - 1 if i % 2 == 0 else clock[0] + 1
        assert tab_drain._presented(tmp_path, entry, snapshot)["label"] == "✋ ordinary"
    snapshot["valid_until"] = 0.0
    for i in range(3):
        clock[0] += 2
        expected = "✋? ordinary" if i == 2 else "✋ ordinary"
        assert tab_drain._presented(tmp_path, entry, snapshot)["label"] == expected
    tab["owner_question"] = True
    assert tab_drain._presented(tmp_path, entry, snapshot)["label"] == "✋ ordinary"


def test_fallback_keeps_confirmed_stale_mark_on_takeover(tmp_path: Path, monkeypatch):
    key = "watch-ordinary"
    entry = {"key": key, "kind": "watch", "label": "◐ ordinary watch", "stale_pending": True}
    state = {"painted": {key: {"label": "◐? ordinary watch", "at": 100.0}}}
    monkeypatch.setattr(tab_drain, "_targets", lambda root: {key: entry})
    assert tab_drain._owed(tmp_path, state) == []
