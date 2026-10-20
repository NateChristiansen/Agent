"""The agent-terminal-focus plugin paints every terminal tab from
`.agent/live.json`, and the `obsidian eval` relabel push stays only as a fallback for one release.

- `live.json` carries a `tabs` entry per live manager and watch tab with the ready label, built by
  the hooks' own `desired_label`, so the plugin copies a string and derives nothing (ADR-0015).
- The plugin paints the label, or the stale label once `valid_until` has passed.
- Its heartbeat (`.agent/tabs/plugin.json`) holds the drainer's push back, and the push resumes
  once the heartbeat is stale.
- Paint errors land in `plugin-metrics.jsonl`, and doctor reports their rate.

The plugin runs in Node against a stub workspace, the pattern of `test_plugin_restart_close.py`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_cli import core, obsidian_tabs, ops, sessions, status, tab_drain, tab_plugin
from agent_cli.doctor import checks_state
from agent_cli.hooks.activity import desired_label, refresh_tab
from agent_cli.vault import live
from agent_cli.vault import managers as managers_mod

PLUGIN = Path(__file__).resolve().parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"
SID = "834e1111-2222-3333-4444-555566667777"
NAME = "mgr-030-tabs"
DISPLAY = "030 tabs · cl 834e"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="Node runs the Obsidian plugin")

# Loads the plugin's TabPainter against a stub workspace of terminal leaves, polls once at `now`
# (epoch ms), and prints each leaf's title and the poll's result. A leaf with `throws` rejects
# a new view state, as a Terminal build that changed its view would.
HARNESS = r"""
const Module = require('module');
const [pluginPath, vault, now, payload] = process.argv.slice(1);
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') return {addIcon() {}, Modal: class {}, Notice: class {}, Plugin: class {}};
  return load.call(this, request, ...rest);
};
const Klass = require(pluginPath);
const leaves = JSON.parse(payload).map(({display, userTitle, throws}) => {
  let state = {userTitle, profile: {}};
  const view = {
    get state() { return state; },
    set state(next) { if (throws) throw new Error('view rejected the state'); state = next; },
  };
  return {view, getDisplayText: () => display || state.userTitle || ''};
});
const workspace = {getLeavesOfType: (type) => (type === 'terminal:terminal' ? leaves : [])};
const painter = new Klass.TabPainter(workspace, vault, '9.9.9');
return (async () => {
const result = await painter.poll(Number(now));
console.log(JSON.stringify({titles: leaves.map((l) => l.view.state.userTitle), result}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""

# One Node process per test module runs every script, each with a fresh module cache, its own
# argv and a captured console, as a `node -e` per poll did at a spawn's cost.
# `test_missing_snapshot_is_quiet_and_later_creation_is_read` still spawns Node itself.
NODE_SERVER = r"""
const Module = require('module');
const load = Module._load;
const reply = (body) => process.stdout.write('\u0000' + JSON.stringify(body) + '\n');
let queue = Promise.resolve();
require('readline').createInterface({input: process.stdin}).on('line', (line) => {
  queue = queue.then(async () => {
    const {script, argv} = JSON.parse(line);
    const out = [];
    const err = [];
    const sink = {log: (...a) => out.push(a.join(' ')), error: (...a) => err.push(a.map(String).join(' '))};
    const proc = Object.create(process);
    proc.argv = [process.argv[0], ...argv];
    proc.exitCode = 0;
    try {
      await new Function('require', 'process', 'console', script)(require, proc, sink);
    } catch (error) {
      err.push(String((error && error.stack) || error));
      proc.exitCode = 1;
    }
    Module._load = load;
    for (const key of Object.keys(require.cache)) delete require.cache[key];
    reply({stdout: out.join('\n'), stderr: err.join('\n'), code: proc.exitCode});
  });
});
"""
_POPEN = subprocess.Popen  # tests patch `tab_drain.subprocess.Popen`, which is this module's
_server: list[subprocess.Popen] = []


@pytest.fixture(autouse=True, scope="module")
def _node_server():
    yield
    for proc in _server:
        proc.kill()
        proc.wait()
    _server.clear()


def _node(script: str, *argv: str) -> str:
    """`script`'s console output, run as `node -e` would with `argv` after the script."""
    if not _server or _server[0].poll() is not None:
        _server[:] = [
            _POPEN(
                ["node", "-e", NODE_SERVER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        ]
    proc = _server[0]
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps({"script": script, "argv": list(argv)}) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    while line and not line.startswith("\0"):
        line = proc.stdout.readline()
    assert line, "the Node server exited"
    reply = json.loads(line[1:])
    assert reply["code"] == 0, reply["stderr"]
    return reply["stdout"]


def _manager(root: Path, monkeypatch, **fields) -> None:
    monkeypatch.setattr(core, "is_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(managers_mod, "claude_live_sessions", lambda: {})
    record = {
        "name": NAME,
        "pid": 4242,
        "provider": "claude",
        "feature": {"id": 30, "slug": "tabs"},
        "activity": {"state": "idle", "at": "2026-10-13T09:00:00-06:00"},
        "tab_display_name": DISPLAY,
        **fields,
    }
    for field, value in record.items():
        sessions.set_field(root, SID, field, value)


def _observed(root: Path) -> float:
    """When the snapshot was derived, on the test clock (`AGENT_NOW`), as epoch seconds."""
    snap = json.loads(live.path(root).read_text(encoding="utf-8"))
    return core.datetime.fromisoformat(snap["observed_at"]).timestamp()


def _paint(vault: Path, leaves: list[dict], now: float | None = None) -> dict:
    """One plugin poll at `now`, by default just after the snapshot was derived."""
    if now is None:
        now = _observed(vault) + 1 if live.path(vault).exists() else time.time()
    return json.loads(_node(HARNESS, str(PLUGIN), str(vault), str(int(now * 1000)), json.dumps(leaves)))


def _metrics(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / tab_plugin.METRICS).read_text(encoding="utf-8").splitlines()]


@NODE
def test_missing_snapshot_is_quiet_and_later_creation_is_read(tmp_path: Path):
    (tmp_path / ".agent").mkdir()
    script = r"""
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const Module = require('module');
const [pluginPath, vault] = process.argv.slice(1);
const load = Module._load;
Module._load = function(request, ...args) {
  if (request === 'obsidian') return {Plugin:class {}, Modal:class {}};
  return load.call(this, request, ...args);
};
const Plugin = require(pluginPath);
const painter = new Plugin.TabPainter({getLeavesOfType:()=>[]}, vault, 'test');
const beat = () => JSON.parse(fs.readFileSync(path.join(vault, '.agent/tabs/plugin.json'), 'utf8'));
(async () => {
  for (const now of [100000, 102000]) {
    const result = await painter.poll(now);
    assert.equal(result.read_error, null);
    assert.equal(result.read_ok, false);
    assert.equal(beat().read_error, null);
    assert.equal(beat().read_ok, false);
    assert.equal(fs.existsSync(path.join(vault, '.agent/tabs/plugin-metrics.jsonl')), false);
  }
  fs.writeFileSync(path.join(vault, '.agent/live.json'), JSON.stringify({tabs:[]}));
  assert.equal((await painter.poll(104000)).read_ok, true);
  assert.equal(beat().read_ok, true);
  assert.equal(beat().read_error, null);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run(
        ["node", "-e", script, str(PLUGIN), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )


# ------------------------------------------------------------------ live.json carries the labels


def test_live_json_carries_each_tabs_ready_label_from_the_hooks_own_code(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch, owner_question_pending=True)
    base = tmp_root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    worker = {"id": "001-mac-01", "feature": 30, "state": "running", "launched_by_session": SID}
    (base / "001-mac-01.json").write_text(json.dumps(worker), encoding="utf-8")
    live.write(tmp_root)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    tabs = {t["key"]: t for t in snap["tabs"]}
    entry = tabs[tab_drain.drain_key("manager", SID)]
    record = sessions.get(tmp_root, SID) or {}
    desired = desired_label(tmp_root, SID, record)
    assert desired is not None and entry["label"] == desired[2] == f"✋ {DISPLAY}"
    assert entry["stale_label"] == f"✋ {DISPLAY}"
    assert entry["owner_question"] is True and entry["stale_reads"] == 3
    # found the way the drainer finds it: last painted name, generated name, then the suffix
    assert (entry["match"], entry["generated_name"], entry["token"]) == (DISPLAY, NAME, " · cl 834e")
    assert entry["valid_until"] == snap["valid_until"] and entry["observed_at"] == snap["observed_at"]
    watch = tabs[tab_drain.drain_key("watch", "001-mac-01")]
    assert watch["label"] == "◐ 001-mac-01 watch" and watch["match"] == "001-mac-01 watch"


def test_an_unclaimed_codex_tab_is_found_by_its_suffix_only(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch, provider="codex", tab_display_name=None)
    entry = live.build(tmp_root)["tabs"][0]
    assert (entry["match"], entry["generated_name"], entry["generated"]) == ("", "", False)
    assert entry["token"] == obsidian_tabs.manager_identity(SID, "codex")


def test_a_label_change_without_a_state_change_rewrites_live_json(tmp_root: Path, monkeypatch):
    """A raised question changes the label, not the activity; the plugin must still see it."""
    monkeypatch.setenv("AGENT_NO_TABS", "1")
    _manager(tmp_root, monkeypatch)
    refresh_tab(tmp_root, SID)
    sessions.set_field(tmp_root, SID, "owner_question_pending", True)
    refresh_tab(tmp_root, SID)
    labels = [t["label"] for t in json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"]]
    assert labels == [f"✋ {DISPLAY}"]
    # an event that changes no label costs a read, not a rebuild
    writes: list[int] = []
    monkeypatch.setattr(live, "write_quietly", lambda root, **kw: writes.append(1))
    refresh_tab(tmp_root, SID)
    assert writes == []


def test_the_stale_label_keeps_the_name_every_matcher_reads():
    assert obsidian_tabs.stale_label(f"✅ +2 {DISPLAY}") == f"✅? +2 {DISPLAY}"
    assert obsidian_tabs.label_name(obsidian_tabs.stale_label(f"✅ +2 {DISPLAY}")) == DISPLAY
    assert obsidian_tabs.stale_label("✅? x") == "✅? x"  # never doubled


# ------------------------------------------------------------------ the plugin paints


@NODE
def test_the_plugin_paints_the_label_from_live_json(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch, owner_question_pending=True)
    live.write(tmp_root)
    leaves = [
        {"userTitle": f"⚙️ {DISPLAY}"},  # this manager's tab, painted ⚙️ earlier
        {"userTitle": "✅ other · cl 9999"},  # another manager's
        {"display": "zsh", "userTitle": ""},  # a plain terminal
    ]
    out = _paint(tmp_root, leaves)
    assert out["titles"] == [f"✋ {DISPLAY}", "✅ other · cl 9999", ""]
    assert out["result"]["attempted"] == out["result"]["painted"] == 1 and out["result"]["stale"] == 0
    beat = tab_plugin.heartbeat(tmp_root)
    assert beat["painted"] == {tab_drain.drain_key("manager", SID): f"✋ {DISPLAY}"}
    assert beat["version"] == "9.9.9"
    assert _metrics(tmp_root)[-1]["failed"] == 0
    # a second poll finds the tab already right: nothing attempted, no metrics row
    again = _paint(tmp_root, [{"userTitle": f"✋ {DISPLAY}"}])
    assert again["result"]["attempted"] == 0 and len(_metrics(tmp_root)) == 1


@NODE
def test_first_expired_poll_keeps_label_until_confirmed(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch)
    live.write(tmp_root)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    until = snap["tabs"][0]["valid_until"]
    later = core.datetime.fromisoformat(until).timestamp() + 1
    out = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=later)
    assert out["titles"] == [f"✅ {DISPLAY}"] and out["result"]["stale"] == 0
    # and before it, the plain label again
    earlier = core.datetime.fromisoformat(until).timestamp() - 1
    assert _paint(tmp_root, [{"userTitle": f"✅? {DISPLAY}"}], now=earlier)["titles"] == [f"✅ {DISPLAY}"]


@NODE
def test_two_tabs_with_one_name_are_left_alone(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch, owner_question_pending=True)
    live.write(tmp_root)
    out = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}, {"userTitle": f"⚙️ {DISPLAY}"}])
    assert out["titles"] == [f"✅ {DISPLAY}", f"⚙️ {DISPLAY}"] and out["result"]["ambiguous"] == 1


@NODE
def test_a_paint_error_is_counted_and_doctor_reports_the_rate(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch, owner_question_pending=True)
    live.write(tmp_root)
    # polled now, so the row falls in doctor's 24 h window (the snapshot is on the test clock)
    out = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}", "throws": True}], now=time.time())
    assert out["titles"] == [f"✅ {DISPLAY}"] and out["result"]["failed"] == 1
    row = _metrics(tmp_root)[-1]
    assert row["attempted"] == 1 and row["failed"] == 1 and "view rejected" in row["errors"][0]
    assert tab_plugin.paint_rate(tmp_root) == {"attempted": 1, "failed": 1, "read_errors": 0}
    [check] = checks_state.plugin_paints(tmp_root)
    assert check["level"] == "warn" and "1/1 plugin tab paints failed" in check["detail"]


@NODE
def test_the_kill_switch_stops_the_plugin_painting(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch, owner_question_pending=True)
    live.write(tmp_root)
    (tmp_root / obsidian_tabs.TABS_OFF_FILE).touch()
    out = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}])
    assert out["titles"] == [f"✅ {DISPLAY}"] and out["result"]["off"] is True


def _worker(root: Path, wid: str, **fields) -> Path:
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    record = {"id": wid, "feature": 30, "launched_by_session": SID, **fields}
    path = base / f"{wid}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _watch_entries(root: Path) -> dict[str, str]:
    return {t["identity"]: t["label"] for t in live.build(root)["tabs"] if t["kind"] == "watch"}


@NODE
@pytest.mark.parametrize(
    ("final", "glyph"),
    [
        ({"state": "cancelled"}, "✳"),
        (
            {
                "state": "done",
                "finished": "2026-09-01T00:00:00+00:00",
                "actioned_at": "2026-09-01T00:01:00+00:00",
            },
            "✳",
        ),
        (
            {
                "state": "failed",
                "finished": "2026-09-01T00:00:00+00:00",
                "actioned_at": "2026-09-01T00:01:00+00:00",
            },
            "✋",
        ),
    ],
)
def test_a_worker_leaving_the_live_set_gets_its_final_paint(tmp_root: Path, monkeypatch, final, glyph):
    """A cancelled worker's watch tab kept ◐ for good, since it
    left `tabs` at once and the plugin's heartbeat held the drainer's final relabel back."""
    _manager(tmp_root, monkeypatch)
    wid = "001-mac-01"
    running = f"◐ {wid} watch"
    _worker(tmp_root, wid, state="running")
    live.write(tmp_root)
    assert _paint(tmp_root, [{"userTitle": running}])["titles"] == [running]
    _worker(tmp_root, wid, **final)
    live.write(tmp_root)
    expected = f"{glyph} {wid} watch"
    assert _watch_entries(tmp_root) == {wid: expected}  # kept, with its final label
    out = _paint(tmp_root, [{"userTitle": running}])
    assert out["titles"] == [expected]
    assert tab_plugin.heartbeat(tmp_root)["painted"][tab_drain.drain_key("watch", wid)] == expected
    assert _watch_entries(tmp_root) == {}  # painted, so dropped


def test_a_final_watch_entry_expires_after_its_grace(tmp_root: Path, monkeypatch):
    """A worker whose watch tab is closed is never painted; its entry still goes."""
    _manager(tmp_root, monkeypatch)
    path = _worker(tmp_root, "001-mac-01", state="cancelled")
    assert _watch_entries(tmp_root) == {"001-mac-01": "✳ 001-mac-01 watch"}
    old = core.now().timestamp() - live.FINAL_PAINT_GRACE_S - 60
    os.utime(path, (old, old))
    assert _watch_entries(tmp_root) == {}


# ------------------------------------------------------------------ the drainer stands back


def _is_runner(argv) -> bool:
    """A drainer launch, not `live`'s intent watchdog, which a raised question starts."""
    return "agent_cli.tab_drain" in argv


def _drainer_enabled(monkeypatch) -> list[int]:
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    launches: list[int] = []

    class Process:
        pid = 99999

    monkeypatch.setattr(
        tab_drain.subprocess,
        "Popen",
        lambda argv, **kw: (_is_runner(argv) and launches.append(1)) or Process(),
    )
    return launches


@NODE
def test_a_fresh_plugin_heartbeat_holds_the_eval_push_back(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch)
    live.write(tmp_root)
    _paint(tmp_root, [], now=time.time())  # the plugin's poll writes its heartbeat
    assert tab_plugin.active(tmp_root)
    launches = _drainer_enabled(monkeypatch)
    out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"⚙️ {DISPLAY}")
    # no painter starts, only a standby runner that first sleeps out the heartbeat
    assert out == {"started": False, "reason": "plugin", "standby": True} and launches == [1]
    # the entry is kept for the fallback, and a drainer already running pushes none of it
    assert tab_drain._desired(tmp_root)[tab_drain.drain_key("manager", SID)]["label"] == f"⚙️ {DISPLAY}"
    assert tab_drain._pending(tmp_root, tab_drain._state(tmp_root)) == []
    # while that runner lives, a further request starts nothing
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (4242, 99999))
    again = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
    assert again == {"started": False, "reason": "plugin"} and launches == [1]
    # an ended Codex tab's reset is not in live.json, so it is still pushed
    sessions.set_field(tmp_root, SID, "activity", {"state": "ended"})
    reset = tab_drain.request(tmp_root, "manager", SID, DISPLAY, "", generated=False, reset=True)
    assert reset["started"] is True


@NODE
def test_the_fallback_paints_a_question_after_the_plugin_stops(tmp_root: Path, monkeypatch):
    """With the plugin fresh, a raised question started no runner,
    and when the plugin then stopped nothing took the ✋ over. The standby runner does."""
    _manager(tmp_root, monkeypatch)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    live.write(tmp_root)
    assert _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=time.time())["titles"] == [f"✅ {DISPLAY}"]
    argvs: list[list[str]] = []

    class Process:
        pid = 99999

    monkeypatch.setattr(
        tab_drain.subprocess,
        "Popen",
        lambda argv, **kw: (_is_runner(argv) and argvs.append(argv)) or Process(),
    )
    ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.")
    assert json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"][0]["label"] == f"✋ {DISPLAY}"
    [argv] = argvs  # one standby runner, due when the heartbeat would go stale
    token, not_before = argv[4], float(argv[5])
    assert not_before == pytest.approx(tab_plugin.expires_at(tmp_root))
    # the plugin stops before its next poll: its heartbeat ages out
    beat = tmp_root / tab_plugin.HEARTBEAT
    beat.write_text(json.dumps({**tab_plugin.heartbeat(tmp_root), "at": time.time() - 60}), encoding="utf-8")
    slept: list[float] = []
    monkeypatch.setattr(tab_drain.time, "sleep", slept.append)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    import agent_cli.obsidian_run as obsidian_run

    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    monkeypatch.setattr(tab_drain, "_eval", lambda entries, timeout=10: {e["key"] for e in entries})
    tab_drain.run(tmp_root, token, not_before)
    assert slept and slept[0] <= tab_plugin.HEARTBEAT_STALE_S + 1  # one bounded sleep, no loop
    painted = tab_drain._state(tmp_root)["painted"][tab_drain.drain_key("manager", SID)]
    assert painted["label"] == f"✋ {DISPLAY}"


@NODE
def test_the_eval_push_resumes_once_the_heartbeat_is_stale(tmp_root: Path, monkeypatch):
    _manager(tmp_root, monkeypatch)
    live.write(tmp_root)
    stale = time.time() - tab_plugin.HEARTBEAT_STALE_S - 1
    _paint(tmp_root, [], now=stale)  # the plugin's last heartbeat, long ago
    assert tab_plugin.heartbeat(tmp_root) and not tab_plugin.active(tmp_root)
    launches = _drainer_enabled(monkeypatch)
    out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"⚙️ {DISPLAY}")
    assert out["started"] is True and launches == [1]
    keys = [e["key"] for e in tab_drain._pending(tmp_root, tab_drain._state(tmp_root))]
    assert keys == [tab_drain.drain_key("manager", SID)]


def test_a_drainer_paint_forgotten_while_the_plugin_painted_is_repainted(tmp_root: Path, monkeypatch):
    """The drainer painted ⚙️ an hour ago; the plugin then painted ✋ and stopped. A request for
    ⚙️ again must not be skipped as already painted: what the tab shows is the newer of the
    plugin's heartbeat and the drainer's record (`_displayed`), and that is ✋."""
    _manager(tmp_root, monkeypatch)
    launches = _drainer_enabled(monkeypatch)
    key = tab_drain.drain_key("manager", SID)
    old = time.time() - 7200
    tab_drain._save_state(tmp_root, {"painted": {key: {"label": f"⚙️ {DISPLAY}", "name": DISPLAY, "at": old}}})
    _beat(tmp_root, time.time(), painted={key: f"✋ {DISPLAY}"})
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
    _beat(tmp_root, time.time() - 3600, painted={key: f"✋ {DISPLAY}"})
    assert (
        tab_drain._displayed(tab_drain._state(tmp_root), tab_plugin.heartbeat(tmp_root), key)
        == f"✋ {DISPLAY}"
    )
    out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"⚙️ {DISPLAY}")
    assert out["started"] is True and launches == [1, 1]  # the standby, then the painter


def _beat(root: Path, at: float, **fields) -> None:
    """A heartbeat as the plugin writes it after a good read at `at`."""
    beat = root / tab_plugin.HEARTBEAT
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"at": at, "read_ok": True, "last_ok_at": at, **fields}), encoding="utf-8")


def test_a_heartbeat_from_the_future_unreadable_or_unhealthy_does_not_count(tmp_root: Path):
    _beat(tmp_root, time.time())
    assert tab_plugin.active(tmp_root)
    _beat(tmp_root, time.time() + 600)
    assert not tab_plugin.active(tmp_root)
    (tmp_root / tab_plugin.HEARTBEAT).write_text('{"at": ', encoding="utf-8")
    assert not tab_plugin.active(tmp_root)
    # polling, but its last poll could not read live.json, or no good read for a while
    _beat(tmp_root, time.time(), read_ok=False)
    assert not tab_plugin.active(tmp_root)
    _beat(tmp_root, time.time(), last_ok_at=time.time() - tab_plugin.HEARTBEAT_STALE_S - 1)
    assert not tab_plugin.active(tmp_root)
    # a build from before `read_ok` paints nothing from live.json either
    (tmp_root / tab_plugin.HEARTBEAT).write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    assert not tab_plugin.active(tmp_root)


@NODE
def test_a_plugin_that_cannot_read_the_snapshot_gives_way_to_the_drainer(tmp_root: Path, monkeypatch):
    """A directory at `.agent/live.json`. The plugin
    polls and fails to read, so its heartbeat says so, and the fallback paints ✋."""
    _manager(tmp_root, monkeypatch)
    live.path(tmp_root).unlink(missing_ok=True)
    live.path(tmp_root).mkdir(parents=True)
    poll = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=time.time())
    assert poll["result"]["read_error"] and poll["result"]["read_ok"] is False
    assert poll["titles"] == [f"✅ {DISPLAY}"]
    beat = tab_plugin.heartbeat(tmp_root)
    assert beat["read_ok"] is False and beat["last_ok_at"] is None
    assert not tab_plugin.active(tmp_root)
    with monkeypatch.context() as patch:
        launches = _drainer_enabled(patch)
        out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
        assert out["started"] is True and launches == [1]  # a painter, not a standby
        labels = [e["label"] for e in tab_drain._pending(tmp_root, tab_drain._state(tmp_root))]
        assert labels == [f"✋ {DISPLAY}"]
    # once it reads again, it is the painter again
    live.path(tmp_root).rmdir()
    live.write(tmp_root)
    again = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=time.time())
    assert again["result"]["read_ok"] is True and tab_plugin.active(tmp_root)


@NODE
def test_a_renewed_heartbeat_rearms_the_standby_for_a_later_request(tmp_root: Path, monkeypatch):
    """The standby woke to a renewed heartbeat and
    exited, stranding a ✋ requested after the renewing poll. It now keeps the duty: it waits
    again, to the new expiry, and paints once the plugin has stopped."""
    _manager(tmp_root, monkeypatch)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    clock = [core.now().timestamp()]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (4242, 99999))
    live.write(tmp_root)
    assert _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0])["titles"] == [f"✅ {DISPLAY}"]
    launches: list[list[str]] = []

    class Process:
        pid = 99999

    monkeypatch.setattr(
        tab_drain, "_spawn", lambda argv, root: (_is_runner(argv) and launches.append(argv)) or Process()
    )
    first = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    assert first["standby"] is True
    token, deadline = launches[0][4], float(launches[0][5])
    clock[0] += 30
    # the plugin renews, then the owner question arrives just after that poll
    _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0])
    sessions.set_field(tmp_root, SID, "owner_question_pending", True)
    live.write(tmp_root)
    second = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
    assert second == {"started": False, "reason": "plugin"}  # the standby is already there
    renewed_expiry = tab_plugin.expires_at(tmp_root)
    slept, paints = _fake_drain(monkeypatch, clock)
    # the old deadline: the renewed heartbeat is fresh and ✋ is owed, so it waits again; the
    # plugin has stopped by then (no more polls), so the second look paints ✋
    clock[0] = deadline
    tab_drain.run(tmp_root, token, deadline)
    assert paints == [[f"✋ {DISPLAY}"]] and len(launches) == 1
    # the standby's own wait (already due), then one more, to the renewed heartbeat's expiry
    assert slept == [0.0, pytest.approx(renewed_expiry - deadline)]
    assert tab_drain._state(tmp_root)["runner"] is None


def test_a_label_the_fallback_painted_turns_stale_at_valid_until(tmp_root: Path, monkeypatch):
    """The fallback paints ⏳ from a fresh running
    snapshot (valid 15 s), its renewal stops, and with the plugin down the same runner waits for
    `valid_until` and repaints ⏳?."""
    from test_obligations import worker

    _manager(tmp_root, monkeypatch)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    worker(tmp_root, state="running", launched_by_session=SID, supervisor_pid=4242)
    live.write(tmp_root)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    until = core.datetime.fromisoformat(snap["valid_until"]).timestamp()
    clock = [core.datetime.fromisoformat(snap["observed_at"]).timestamp()]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    launches = _drainer_enabled(monkeypatch)
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"⏳ {DISPLAY}")
    assert launches == [1]
    state = tab_drain._state(tmp_root)
    slept, paints = _fake_drain(monkeypatch, clock)
    tab_drain.run(tmp_root, state["runner_token"])
    assert paints == [[f"⏳ {DISPLAY}"], [f"⏳? {DISPLAY}"]]
    assert clock[0] == pytest.approx(until + 5.5, abs=0.01, rel=0) and len(slept) == 3
    assert tab_drain._state(tmp_root)["runner"] is None


def _fake_drain(monkeypatch, clock: list[float]) -> tuple[list[float], list[list[str]]]:
    """A drainer whose waits advance `clock` and whose paints are recorded, never evaluated."""
    import agent_cli.obsidian_run as obsidian_run

    slept: list[float] = []
    paints: list[list[str]] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    monkeypatch.setattr(
        tab_drain,
        "_apply",
        lambda root, entries: (
            # the manager tab's paints; a worker's watch tab is a target too, painted alongside
            paints.append([e["label"] for e in entries if e["kind"] == "manager"])
            if any(e["kind"] == "manager" for e in entries)
            else None,
            ({e["key"] for e in entries}, set()),
        )[1],
    )
    return slept, paints


def test_a_failed_spawn_cannot_strand_an_owed_paint(tmp_root: Path, monkeypatch):
    """The heartbeat renewed just before the
    standby's deadline, and launching a successor failed, so the runner released and left ✋
    unpainted. The runner now waits in-process, so no launch is needed: it paints after expiry."""
    _manager(tmp_root, monkeypatch, owner_question_pending=True)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    clock = [time.time()]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    live.write(tmp_root)
    # the plugin found the tab and shows ✅; it has not painted the owed ✋
    shows = {tab_drain.drain_key("manager", SID): f"✅ {DISPLAY}"}
    _beat(tmp_root, clock[0], painted=shows)
    launches: list[list[str]] = []

    class Process:
        pid = 99999

    def spawn(argv, root):
        if not _is_runner(argv):
            return Process()
        if launches:
            raise OSError("temporary process capacity failure")
        launches.append(argv)
        return Process()

    monkeypatch.setattr(tab_drain, "_spawn", spawn)
    out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
    assert out["standby"] is True
    deadline = float(launches[0][5])
    _beat(tmp_root, deadline - 1, painted=shows)  # renewed just before the deadline
    clock[0] = deadline
    slept, paints = _fake_drain(monkeypatch, clock)
    tab_drain.run(tmp_root, launches[0][4], deadline, 0)
    assert paints[0] == [f"✋ {DISPLAY}"] and len(launches) == 1
    assert tab_drain._state(tmp_root)["runner"] is None


@NODE
def test_the_fallback_paints_the_stale_label_past_valid_until(tmp_root: Path, monkeypatch):
    """With the snapshot expired, the fallback painted
    the definitive ⏳ through the real batch JavaScript. It now paints the snapshot's ⏳?."""
    from test_obligations import worker

    _manager(tmp_root, monkeypatch)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    clock = [core.now().timestamp()]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    worker(tmp_root, state="running", launched_by_session=SID, supervisor_pid=4242)
    live.write(tmp_root)
    snapshot = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    tab = next(t for t in snapshot["tabs"] if t["identity"] == SID)
    assert tab["label"] == f"⏳ {DISPLAY}"
    monkeypatch.delenv("AGENT_NOW", raising=False)
    _beat(tmp_root, clock[0], painted={})
    launches: list[list[str]] = []

    class Process:
        pid = 99999

    monkeypatch.setattr(
        tab_drain, "_spawn", lambda argv, root: (_is_runner(argv) and launches.append(argv)) or Process()
    )
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, tab["label"])
    clock[0] = float(launches[0][5])
    assert core.datetime.fromisoformat(snapshot["valid_until"]).timestamp() < clock[0]
    import agent_cli.obsidian_run as obsidian_run

    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    titles: list[str] = []

    def execute_batch(entries, timeout=10):  # one manager tab; the worker's watch tab is not open
        code = tab_drain._batch_code(entries)
        script = (
            "const view = {state: {userTitle: " + json.dumps(f"✅ {DISPLAY}") + "}};"
            "const app = {workspace: {getLeavesOfType: () => [{view, getDisplayText: () => view.state.userTitle}]}};"
            "const result = " + code + ";"
            "console.log(JSON.stringify({result, title: view.state.userTitle}));"
        )
        out = json.loads(_node(script))
        titles.append(out["title"])
        return set(out["result"].removeprefix("AGENT_TAB_RESULTS:").split(","))

    monkeypatch.setattr(tab_drain, "_eval", execute_batch)
    tab_drain.run(tmp_root, launches[0][4], clock[0])
    assert titles[0] == f"⏳ {DISPLAY}"  # first expired read; no stale mark yet
    assert tab["stale_label"] == f"⏳? {DISPLAY}" and tab["stale_label"] in titles
    painted = tab_drain._state(tmp_root)["painted"][tab_drain.drain_key("manager", SID)]
    assert painted["label"] == f"⏳? {DISPLAY}"
    # the same request again is already shown in its stale form: nothing more to paint
    assert tab_drain._pending(tmp_root, tab_drain._state(tmp_root)) == []


def test_a_plugin_that_never_paints_is_overruled_after_the_rearm_limit(tmp_root: Path, monkeypatch):
    """A fresh, healthy heartbeat whose `painted` never shows the owed label: the runner waits
    `MAX_REARMS` times, then paints the request itself."""
    _manager(tmp_root, monkeypatch, owner_question_pending=True)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    live.write(tmp_root)
    label = f"✋ {DISPLAY}"
    assert json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"][0]["label"] == label
    _beat(tmp_root, time.time(), painted={tab_drain.drain_key("manager", SID): f"✅ {DISPLAY}"})
    launches: list[list[str]] = []

    class Process:
        pid = 99999

    monkeypatch.setattr(
        tab_drain, "_spawn", lambda argv, root: (_is_runner(argv) and launches.append(argv)) or Process()
    )
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, label)
    clock = [time.time()]
    slept, paints = _fake_drain(monkeypatch, clock)
    # the plugin keeps polling (heartbeat renewed at every wake) but never paints ✋
    monkeypatch.setattr(
        tab_drain.time,
        "sleep",
        lambda seconds: (
            slept.append(seconds),
            _beat(tmp_root, time.time(), painted={tab_drain.drain_key("manager", SID): f"✅ {DISPLAY}"}),
        ),
    )
    tab_drain.run(tmp_root, launches[0][4], time.time())
    assert len(slept) == tab_drain.MAX_REARMS + 1  # the first wait, then one per re-arm
    assert paints == [[label]] and len(launches) == 1


def test_a_plugin_painting_the_published_snapshot_is_not_overruled_by_a_write_in_flight(
    tmp_root: Path, monkeypatch
):
    """A drain under a fresh heartbeat: every snapshot write
    stands an intent, so the drainer derives its own view while the plugin paints live.json. Past
    `valid_until` the plugin shows the published `?` form and the derived view the plain label,
    so the key read as the plugin failing it at every look, and after `MAX_REARMS` it was painted
    through an eval."""
    from test_obligations import worker

    _manager(tmp_root, monkeypatch)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    clock = [core.now().timestamp()]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    monkeypatch.setattr(core, "now", lambda: core.datetime.fromtimestamp(clock[0]).astimezone())
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (4242, 99999, os.getpid()))
    worker(tmp_root, state="running", launched_by_session=SID, supervisor_pid=4242)
    live.write(tmp_root)
    key = tab_drain.drain_key("manager", SID)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    [tab] = [t for t in snap["tabs"] if t["key"] == key]
    clock[0] = core.datetime.fromisoformat(snap["valid_until"]).timestamp() + 60

    def plugin_polls_while_a_write_is_in_flight() -> None:
        _beat(tmp_root, clock[0], painted={key: tab["stale_label"]}, protocol=tab_plugin.YIELD_PROTOCOL)
        for old in (tmp_root / live.WRITE_FAILED).glob(f"{live.INTENT_PREFIX}*"):
            old.unlink()
        live.begin_intent(tmp_root)

    plugin_polls_while_a_write_is_in_flight()
    assert tab_plugin.active(tmp_root)
    launches: list[list[str]] = []

    class Process:
        pid = 99999

    monkeypatch.setattr(
        tab_drain, "_spawn", lambda argv, root: (_is_runner(argv) and launches.append(argv)) or Process()
    )
    assert tab_drain.request(tmp_root, "manager", SID, DISPLAY, tab["label"])["standby"] is True
    slept, paints = _fake_drain(monkeypatch, clock)

    def sleep(seconds: float) -> None:
        # unfixed, the eval and the plugin repaint the tab in turn, and the runner never exits
        assert len(paints) < 3, paints
        slept.append(seconds)
        clock[0] += seconds
        plugin_polls_while_a_write_is_in_flight()

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[0][4], float(launches[0][5]))
    assert paints == [] and len(slept) == 1
    assert tab_drain._state(tmp_root)["runner"] is None


@pytest.fixture
def short_lock_waits(monkeypatch) -> None:
    """A busy request gives up on a held `drainer.lock` in real time; a fifth of it is the same path."""
    monkeypatch.setattr(tab_drain, "REQUEST_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(tab_drain, "REQUEST_BUDGET_S", 0.4)


def _real_drain(root: Path, monkeypatch, *, running: bool) -> tuple[list[float], list[list[str]], list[str]]:
    """A fake clock driving both the drainer and `core.now`, no
    plugin, and the drainer's real batch JavaScript run in Node against one tab."""
    import agent_cli.obsidian_run as obsidian_run
    from test_obligations import worker

    _manager(root, monkeypatch)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    clock = [core.now().timestamp()]
    monkeypatch.setattr(tab_drain.time, "time", lambda: clock[0])
    monkeypatch.setattr(core, "now", lambda: core.datetime.fromtimestamp(clock[0]).astimezone())
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (4242, 99999, os.getpid()))
    if running:
        worker(root, state="running", launched_by_session=SID, supervisor_pid=4242)
    live.write(root)
    launches: list[list[str]] = []

    class Process:
        pid = 99999

    monkeypatch.setattr(
        tab_drain, "_spawn", lambda argv, root: (_is_runner(argv) and launches.append(argv)) or Process()
    )
    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    titles = [f"✅ {DISPLAY}"]

    def execute(entries, timeout=10):
        script = (
            "const view = {state: {userTitle: " + json.dumps(titles[-1]) + "}};"
            "const app = {workspace: {getLeavesOfType: () => [{view, getDisplayText: () => view.state.userTitle}]}};"
            "const result = " + tab_drain._batch_code(entries) + ";"
            "console.log(JSON.stringify({result, title: view.state.userTitle}));"
        )
        result = json.loads(_node(script))
        titles.append(result["title"])
        return set(result["result"].removeprefix("AGENT_TAB_RESULTS:").split(","))

    monkeypatch.setattr(tab_drain, "_eval", execute)
    return clock, launches, titles


@NODE
def test_renewals_keep_the_runner_until_the_last_valid_until(tmp_root: Path, monkeypatch):
    """Three renewals used up `MAX_EXPIRY_WAITS`, the
    runner left, and when the heartbeats stopped the tab kept a definitive ⏳ past the expiry. A
    renewal is now progress: the runner stays while they come, and paints ⏳? just after the last
    `valid_until`."""
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=True)
    label = f"⏳ {DISPLAY}"
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, label)
    sleeps: list[float] = []
    renewing = 5  # the supervisor writes heartbeats through five waits, then stops

    def sleep(seconds: float) -> None:
        if seconds > 1:  # an expiry wait; the short ones pace retries of the unopened watch tab
            sleeps.append(seconds)
        target = clock[0] + seconds
        while len(sleeps) <= renewing and clock[0] + live.HEARTBEAT_SECONDS < target:
            clock[0] += live.HEARTBEAT_SECONDS
            live.write(tmp_root, heartbeat=True)
        clock[0] = target

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[0][4])
    final = core.datetime.fromisoformat(
        json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["valid_until"]
    ).timestamp()
    assert len(sleeps) == renewing + 3 > tab_drain.MAX_EXPIRY_WAITS  # renewals plus three stale reads
    shown = [t for i, t in enumerate(titles) if i == 0 or t != titles[i - 1]]
    assert shown == [f"✅ {DISPLAY}", label, f"⏳? {DISPLAY}"]
    assert clock[0] == pytest.approx(final + 0.5)  # painted half a second after the expiry
    assert tab_drain._state(tmp_root)["runner"] is None


def test_a_lifetime_cap_ends_a_runner_that_renewals_would_keep(tmp_root: Path, monkeypatch):
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=True)
    monkeypatch.setattr(tab_drain, "MAX_LIFETIME_S", 60.0)
    monkeypatch.setattr(tab_drain, "_eval", lambda entries, timeout=10: {e["key"] for e in entries})
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"⏳ {DISPLAY}")

    def sleep(seconds: float) -> None:  # the writer never stops
        target = clock[0] + seconds
        while clock[0] + live.HEARTBEAT_SECONDS < target:
            clock[0] += live.HEARTBEAT_SECONDS
            live.write(tmp_root, heartbeat=True)
        clock[0] = target

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[0][4])
    assert tab_drain._state(tmp_root)["runner"] is None
    rows = [
        json.loads(line)
        for line in (tmp_root / tab_drain.TABS_DIR / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["event"] == "lifetime-cap"


@NODE
def test_the_same_request_after_an_idle_expiry_paints_the_stale_form(tmp_root: Path, monkeypatch):
    """After the 12 h idle snapshot expired, the same
    request was skipped as unchanged against the definitive paint. It now compares the label
    the snapshot permits, ✅?, starts a runner and paints it."""
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    label = f"✅ {DISPLAY}"
    titles[0] = f"⚙️ {DISPLAY}"
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, label)
    tab_drain.run(tmp_root, launches[0][4])
    assert titles[-1] == label
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    clock[0] = core.datetime.fromisoformat(snap["valid_until"]).timestamp() + 11
    out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, label)
    assert out == {"started": True, "queued": True} and len(launches) == 2
    tab_drain.run(tmp_root, launches[1][4])
    assert titles[-1] == f"✅? {DISPLAY}"
    # and now it is done: the same request again changes nothing
    assert tab_drain.request(tmp_root, "manager", SID, DISPLAY, label)["reason"] == "unchanged"


def _plugin_stops_then_supervisor(root: Path, monkeypatch, clock, launches, titles) -> None:
    """The plugin stops polling. The supervisor's 5 s heartbeat (`live.write_quietly`, as
    `driver.py` calls it) goes on until the plugin counts as unhealthy, then it stops too. The
    expiry guard must have started one runner, which then paints the stale form."""
    started = len(launches)
    stop = clock[0]
    while clock[0] < stop + tab_plugin.HEARTBEAT_STALE_S + 10:
        clock[0] += live.HEARTBEAT_SECONDS
        live.write_quietly(root, heartbeat=True)
    assert not tab_plugin.active(root)
    assert len(launches) == started + 1  # the guard started one runner, once
    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    tab_drain.run(root, launches[-1][4])
    until = core.datetime.fromisoformat(
        json.loads(live.path(root).read_text(encoding="utf-8"))["valid_until"]
    ).timestamp()
    assert titles[-1] == f"⏳? {DISPLAY}"
    assert until + 4 <= clock[0] < until + 20  # three stale reads, then missing watch-tab retries
    assert tab_drain._state(root)["runner"] is None


@NODE
def test_a_label_the_plugin_painted_is_guarded_when_the_plugin_stops(tmp_root: Path, monkeypatch):
    """The healthy plugin painted ⏳, so the standby found nothing
    owed and released. When the plugin stopped, nothing guarded the expiry. The snapshot's
    writer now calls `ensure_expiry_guard`, which starts a runner once the plugin is unhealthy."""
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=True)
    label = f"⏳ {DISPLAY}"
    titles.append(_paint(tmp_root, [{"userTitle": titles[-1]}], now=clock[0])["titles"][0])
    assert titles[-1] == label and tab_plugin.active(tmp_root)
    out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, label)
    assert out["standby"]

    def sleep(seconds: float) -> None:  # the plugin and the supervisor are both alive
        target = clock[0] + seconds
        while clock[0] + live.HEARTBEAT_SECONDS < target:
            clock[0] += live.HEARTBEAT_SECONDS
            live.write_quietly(tmp_root, heartbeat=True)
            titles.append(_paint(tmp_root, [{"userTitle": titles[-1]}], now=clock[0])["titles"][0])
        clock[0] = target

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[0][4], float(launches[0][5]))
    assert tab_drain._state(tmp_root)["runner"] is None and titles[-1] == label
    # a healthy plugin needs no runner: the writes so far started none
    assert len(launches) == 1
    _plugin_stops_then_supervisor(tmp_root, monkeypatch, clock, launches, titles)


@NODE
def test_a_recovered_plugin_hands_expiry_back_when_it_stops(tmp_root: Path, monkeypatch):
    """The fallback was waiting for expiry when the
    plugin recovered and took over; the runner left. When the plugin then stopped, the guard
    must take the expiry back."""
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=True)
    label = f"⏳ {DISPLAY}"
    assert tab_drain.request(tmp_root, "manager", SID, DISPLAY, label)["started"]

    def sleep(seconds: float) -> None:
        target = clock[0] + seconds
        clock[0] += live.HEARTBEAT_SECONDS
        live.write_quietly(tmp_root, heartbeat=True)
        # the plugin recovers while the fallback waits for expiry
        titles.append(_paint(tmp_root, [{"userTitle": titles[-1]}], now=clock[0])["titles"][0])
        clock[0] = target

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[0][4])
    assert tab_plugin.active(tmp_root) and titles[-1] == label
    assert tab_drain._state(tmp_root)["runner"] is None
    _plugin_stops_then_supervisor(tmp_root, monkeypatch, clock, launches, titles)


def test_the_guard_starts_nothing_while_the_plugin_is_healthy_or_nothing_definitive_shows(
    tmp_root: Path, monkeypatch
):
    """An idle snapshot (valid 12 h): the guard starts a runner only for an unhealthy plugin and
    a paint owed now, never for a healthy one or a tab already showing the snapshot's label."""
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    key = tab_drain.drain_key("manager", SID)
    _beat(tmp_root, clock[0], painted={key: f"⚙️ {DISPLAY}"})
    assert tab_drain.ensure_expiry_guard(tmp_root) is False  # healthy plugin: it repaints
    _beat(tmp_root, clock[0], read_ok=False, painted={key: f"✅ {DISPLAY}"})
    assert tab_drain.ensure_expiry_guard(tmp_root) is False  # shows the label; expiry far off
    _beat(tmp_root, clock[0], read_ok=False, painted={key: f"⚙️ {DISPLAY}"})
    assert tab_drain.ensure_expiry_guard(tmp_root) is True and len(launches) == 1  # ✅ owed
    assert tab_drain.ensure_expiry_guard(tmp_root) is False  # one runner at a time


# ------------------------------------------------------------------ the fallback is the plugin's rule


def _plugin_paints(root: Path, clock: list[float], titles: list[str]) -> None:
    """One poll of the real plugin against the open tabs `titles` (updated in place)."""
    titles[:] = _paint(root, [{"userTitle": title} for title in titles], now=clock[0])["titles"]


def _healthy_sleep(root: Path, clock: list[float], titles: list[str]):
    """A wait during which the supervisor writes heartbeats and the plugin polls."""

    def sleep(seconds: float) -> None:
        target = clock[0] + seconds
        while clock[0] + live.HEARTBEAT_SECONDS < target:
            clock[0] += live.HEARTBEAT_SECONDS
            live.write_quietly(root, heartbeat=True)
            _plugin_paints(root, clock, titles)
        clock[0] = target

    return sleep


def _batch_on(titles: list[str]):
    """The drainer's real batch JavaScript, run in Node against the open tabs `titles`."""

    def execute(entries, timeout=10):
        script = (
            "const leaves = " + json.dumps(titles) + ".map(title => {"
            "const view = {state: {userTitle: title}};"
            "return {view, getDisplayText: () => view.state.userTitle};});"
            "const app = {workspace: {getLeavesOfType: () => leaves}};"
            "const result = " + tab_drain._batch_code(entries) + ";"
            "console.log(JSON.stringify({result, titles: leaves.map(l => l.view.state.userTitle)}));"
        )
        output = json.loads(_node(script))
        titles[:] = output["titles"]
        return set(output["result"].removeprefix("AGENT_TAB_RESULTS:").split(",")) - {""}

    return execute


def _stop_plugin(root: Path, clock: list[float]) -> None:
    """The plugin stops polling; the supervisor's heartbeat goes on until it counts unhealthy."""
    stopped_at = clock[0]
    while clock[0] < stopped_at + tab_plugin.HEARTBEAT_STALE_S + 10:
        clock[0] += live.HEARTBEAT_SECONDS
        live.write_quietly(root, heartbeat=True)
    assert not tab_plugin.active(root)


@NODE
def test_takeover_paints_the_snapshot_not_an_obsolete_request(tmp_root: Path, monkeypatch):
    """A saved ✅ request outlived the snapshot's ⏳
    (a worker started; no manager hook ran), and the takeover painted ✅ over ⏳. The fallback
    now paints the snapshot: ⏳ stays, and turns ⏳? at expiry."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=False)
    titles = [f"✅ {DISPLAY}"]
    _plugin_paints(tmp_root, clock, titles)
    assert tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")["standby"]
    monkeypatch.setattr(tab_drain.time, "sleep", _healthy_sleep(tmp_root, clock, titles))
    tab_drain.run(tmp_root, launches[0][4], float(launches[0][5]))
    from test_obligations import worker

    worker(tmp_root, state="running", launched_by_session=SID, supervisor_pid=4242)
    live.write_quietly(tmp_root, heartbeat=True)
    _plugin_paints(tmp_root, clock, titles)
    assert titles == [f"⏳ {DISPLAY}"]
    assert tab_drain._desired(tmp_root)[tab_drain.drain_key("manager", SID)]["label"] == f"✅ {DISPLAY}"
    _stop_plugin(tmp_root, clock)
    assert len(launches) == 2  # the guard's runner
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    painted: list[str] = []

    def sleep(seconds: float) -> None:
        painted.append(titles[0])
        clock[0] += seconds

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[-1][4])
    assert f"✅ {DISPLAY}" not in painted and titles == [f"⏳? {DISPLAY}"]
    assert tab_drain._state(tmp_root)["runner"] is None


@NODE
def test_a_plugin_painted_watch_tab_turns_stale_with_no_saved_request(tmp_root: Path, monkeypatch):
    """A watch tab the plugin painted had no saved
    request, so the takeover left ◐ definitive past expiry. Every snapshot tab is a target."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=True)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    watch = next(t for t in snap["tabs"] if t["kind"] == "watch")
    titles = [f"⏳ {DISPLAY}", watch["label"]]
    _plugin_paints(tmp_root, clock, titles)
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"⏳ {DISPLAY}")
    assert watch["key"] not in tab_drain._desired(tmp_root)
    monkeypatch.setattr(tab_drain.time, "sleep", _healthy_sleep(tmp_root, clock, titles))
    tab_drain.run(tmp_root, launches[0][4], float(launches[0][5]))
    _stop_plugin(tmp_root, clock)
    assert len(launches) == 2
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    tab_drain.run(tmp_root, launches[-1][4])
    assert titles == [f"⏳? {DISPLAY}", watch["stale_label"]] == [f"⏳? {DISPLAY}", f"◐? {watch['match']}"]
    assert tab_drain._state(tmp_root)["runner"] is None


@NODE
def test_plugin_only_paints_are_guarded_with_no_saved_request(tmp_root: Path, monkeypatch):
    """No saved request at all, and writers that
    went on started no guard. The guard now reads the snapshot's tabs."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=True)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    watch = next(t for t in snap["tabs"] if t["kind"] == "watch")
    titles = [f"✅ {DISPLAY}", watch["label"]]
    _plugin_paints(tmp_root, clock, titles)
    assert titles[0] == f"⏳ {DISPLAY}" and tab_drain._desired(tmp_root) == {}
    _stop_plugin(tmp_root, clock)
    assert len(launches) == 1  # the guard started one runner from the snapshot alone
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    tab_drain.run(tmp_root, launches[0][4])
    assert titles == [f"⏳? {DISPLAY}", watch["stale_label"]]


@NODE
def test_an_already_expired_snapshot_is_painted_stale_while_writes_fail(tmp_root: Path, monkeypatch):
    """The plugin stopped and every snapshot write
    failed, so the snapshot expired before the plugin counted unhealthy, and the guard, which
    waited only for a future expiry, started nothing. Expired now is due now. Since then the
    fallback paints facts it derives itself while writes fail, so the tabs show the current
    facts (definitive, since they are known), not the expired snapshot's `?` form."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=True)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    watch = next(t for t in snap["tabs"] if t["kind"] == "watch")
    titles = [f"✅ {DISPLAY}", watch["label"]]
    _plugin_paints(tmp_root, clock, titles)
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"⏳ {DISPLAY}")
    monkeypatch.setattr(tab_drain.time, "sleep", _healthy_sleep(tmp_root, clock, titles))
    tab_drain.run(tmp_root, launches[0][4], float(launches[0][5]))
    assert tab_drain._state(tmp_root)["runner"] is None and len(launches) == 1
    writes: list[float] = []

    def failing_write(root, *, heartbeat=False):
        writes.append(clock[0])
        raise OSError("snapshot destination temporarily unwritable")

    monkeypatch.setattr(live, "write", failing_write)
    _stop_plugin(tmp_root, clock)
    until = core.datetime.fromisoformat(
        json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["valid_until"]
    ).timestamp()
    assert writes and until < clock[0] - 10
    assert len(launches) == 2  # the first write after the plugin went unhealthy
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    # the fake clock walks to the lifetime cap in 15 s renewals; a shorter cap keeps the slower
    # file operations on Windows from turning that walk into minutes
    monkeypatch.setattr(tab_drain, "MAX_LIFETIME_S", 600.0)
    tab_drain.run(tmp_root, launches[-1][4])
    assert titles == [f"⏳ {DISPLAY}", watch["label"]]  # the derived facts: the worker still runs
    assert tab_drain._state(tmp_root)["runner"] is None


def _failing_writes(monkeypatch) -> None:
    from agent_cli.vault import live as live_mod

    def fail_write(root: Path, *, heartbeat: bool = False) -> bool:
        raise OSError("snapshot destination temporarily unwritable")

    monkeypatch.setattr(live_mod, "write", fail_write)


@NODE
def test_a_question_raised_while_rewrites_fail_reaches_the_tab(tmp_root: Path, monkeypatch):
    """Every snapshot rewrite failed, a question was
    raised, and the fallback kept painting the stale snapshot's ✅ over the saved ✋. A failed
    rewrite makes the snapshot non-authoritative, and the newer saved request wins its key."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=False)
    titles = [f"⚙️ {DISPLAY}"]
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert tab_drain.ensure_expiry_guard(tmp_root)
    tab_drain.run(tmp_root, launches[-1][4])
    assert titles == [f"✅ {DISPLAY}"]
    started = len(launches)
    _failing_writes(monkeypatch)
    clock[0] += 1
    ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.")
    for _ in range(8):
        clock[0] += live.HEARTBEAT_SECONDS
        live.write_quietly(tmp_root, heartbeat=True)
    key = tab_drain.drain_key("manager", SID)
    assert json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["needs_you"] == []  # the old snapshot
    assert live.write_failed(tmp_root) is not None
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"
    assert len(launches) == started + 1  # the saved ✋ is owed, so a runner started
    tab_drain.run(tmp_root, launches[-1][4])
    assert titles == [f"✋ {DISPLAY}"]


def test_status_check_calls_a_snapshot_stale_after_a_failed_rewrite(tmp_root: Path, monkeypatch, run):
    """A known-failed rewrite after the snapshot
    was derived makes it a stale view, though its `valid_until` is ahead."""
    from agent_cli import status

    _manager(tmp_root, monkeypatch)
    assert status.render_quietly(tmp_root)
    assert status.check(tmp_root)["fresh"] is True
    with monkeypatch.context() as patch:
        _failing_writes(patch)
        ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.")
        assert not status.render_quietly(tmp_root, source="probe")
    view = status.check(tmp_root)["views"][live.path(tmp_root).relative_to(tmp_root).as_posix()]
    assert view == {**view, "fresh": False, "found": "a later rewrite failed"}
    assert status.check(tmp_root)["fresh"] is False
    assert run(["status", "check"]).code != 0
    # a successful rewrite clears it
    assert status.render_quietly(tmp_root)
    assert live.write_failed(tmp_root) is None and status.check(tmp_root)["fresh"] is True


@NODE
def test_a_saved_request_the_snapshot_does_not_list_turns_stale(tmp_root: Path, monkeypatch):
    """A second manager the snapshot did not list
    (every rewrite failed) kept its saved ⚙️ definitive past expiry. Saved requests carry the
    same validity contract. Since then, while rewrites fail, the fallback derives the facts
    itself, and they list the second manager: its tab shows its current, known label."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=True)
    second = "abcd1111-2222-3333-4444-555566667777"
    for field, value in {
        "name": "mgr-031-second",
        "pid": 4242,
        "provider": "claude",
        "activity": {"state": "working", "at": core.iso(core.now())},
        "tab_display_name": "031 second · cl abcd",
    }.items():
        sessions.set_field(tmp_root, second, field, value)
    _failing_writes(monkeypatch)
    titles = [f"✅ {DISPLAY}", "✅ 031 second · cl abcd"]
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    from agent_cli.hooks.activity import refresh_tab

    refresh_tab(tmp_root, second)
    assert launches
    monkeypatch.setattr(tab_drain, "MAX_LIFETIME_S", 600.0)
    tab_drain.run(tmp_root, launches[-1][4])
    second_key = tab_drain.drain_key("manager", second)
    assert second_key not in json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"]
    assert tab_drain._targets(tmp_root)[second_key]["source"] == "snapshot"  # from the derived facts
    assert titles[0] == f"⏳ {DISPLAY}"
    assert titles[1].startswith("⚙️ ") and titles[1].endswith(" · cl abcd")
    assert tab_drain._state(tmp_root)["runner"] is None


@NODE
def test_a_healthy_plugin_yields_to_the_fallback_while_rewrites_fail(tmp_root: Path, monkeypatch):
    """With rewrites failing, the healthy plugin kept
    painting the old snapshot's ✅ and the fallback stood back, so ✋ never showed. The plugin now
    sees the failure record, stops painting and says `yielded`; the fallback paints ✋; a good
    rewrite ends it and the plugin paints again. One painter at a time throughout.

    And promptly with the harness idle: no supervisor writes; the runner the
    failed rewrite started polls for the yield and paints ✋ within 10 s of the question."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=False)
    titles = [f"⚙️ {DISPLAY}"]
    _plugin_paints(tmp_root, clock, titles)
    assert titles == [f"✅ {DISPLAY}"] and tab_plugin.active(tmp_root)
    clock[0] += 1
    with monkeypatch.context() as patch:
        _failing_writes(patch)
        ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.")
        asked = clock[0]
        key = tab_drain.drain_key("manager", SID)
        assert live.write_failed(tmp_root) is not None
        assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"
        # the plugin has not yet yielded, but a runner starts now (no standby 30 s away)
        assert tab_plugin.active(tmp_root) and len(launches) == 1 and launches[0][5:] == []
        monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
        seen: list[tuple[float, str, bool]] = []

        def sleep(seconds: float) -> None:  # the plugin keeps polling every 2 s; nothing else runs
            clock[0] += seconds
            _plugin_paints(tmp_root, clock, titles)
            seen.append((clock[0], titles[0], bool(tab_plugin.heartbeat(tmp_root)["yielded"])))

        monkeypatch.setattr(tab_drain.time, "sleep", sleep)
        tab_drain.run(tmp_root, launches[0][4])
        assert tab_plugin.heartbeat(tmp_root)["yielded"] is True and not tab_plugin.active(tmp_root)
        assert titles == [f"✋ {DISPLAY}"] and clock[0] - asked <= 10
        # the plugin painted nothing while it had not yielded, and the runner nothing before it did
        assert all(title == f"✅ {DISPLAY}" for _, title, _ in seen)
    clock[0] += 1
    assert live.write(tmp_root) and live.write_failed(tmp_root) is None  # a good rewrite
    _plugin_paints(tmp_root, clock, titles)
    assert titles == [f"✋ {DISPLAY}"] and tab_plugin.active(tmp_root)
    assert tab_plugin.heartbeat(tmp_root)["yielded"] is False


def test_an_older_success_cannot_erase_a_newer_failure(tmp_root: Path, monkeypatch):
    """A writer that derived its
    snapshot first, then published slowly, cleared the failure recorded by a newer writer, and
    `status check` called the old snapshot fresh. The publish lock and the derivation order keep
    the newer failure standing."""
    import threading

    from agent_cli import status

    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(live, "PUBLISH_WAIT_S", 0.2)
    assert status.render_quietly(tmp_root)
    clock[0] += live.HEARTBEAT_SECONDS
    published, finish = threading.Event(), threading.Event()
    original = core.atomic_write
    errors: list[BaseException] = []

    def slow_publish(path: Path, body: str) -> None:
        if path == live.path(tmp_root) and threading.current_thread().name == "older":
            original(path, body)
            published.set()
            assert finish.wait(10)  # still inside the publish lock
            return
        if path == live.path(tmp_root):
            raise OSError("snapshot destination temporarily unwritable")
        original(path, body)

    def older() -> None:
        try:
            assert live.write(tmp_root, heartbeat=True)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(core, "atomic_write", slow_publish)
    thread = threading.Thread(target=older, name="older")
    thread.start()
    try:
        assert published.wait(10)
        clock[0] += 1
        ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.")
        assert live.write_failed(tmp_root) is not None
    finally:
        finish.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    monkeypatch.setattr(core, "atomic_write", original)
    assert live.write_failed(tmp_root) is not None  # the older success left the newer failure
    assert status._live_state(tmp_root) == "a later rewrite failed"
    assert status.check(tmp_root)["fresh"] is False
    key = tab_drain.drain_key("manager", SID)
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"
    # a success derived after the failure clears it
    clock[0] += 1
    assert live.write(tmp_root) and live.write_failed(tmp_root) is None


@NODE
def test_a_clock_rollback_does_not_hold_a_question_back(tmp_root: Path, monkeypatch):
    """With the clock stepped back a minute, writes
    were refused as older than the snapshot on disk, and ✋ waited a minute. No timestamp orders
    writes now: the lock does, so the question publishes at once."""
    from agent_cli import status

    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    assert status.render_quietly(tmp_root)
    clock[0] -= 60
    assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    assert snap["needs_you"] and snap["tabs"][0]["label"] == f"✋ {DISPLAY}"
    titles = [f"✅ {DISPLAY}"]
    _plugin_paints(tmp_root, clock, titles)
    assert titles == [f"✋ {DISPLAY}"]


def _slow_writer(monkeypatch, pause: str, released_after: float | threading.Event):
    """A `live.write` from a thread named "old" that holds `pause` (`build` or the managers read)
    until a timer, or the test setting `released_after`, releases it: a slow writer that captured
    its facts first."""
    from agent_cli.vault import managers as managers_mod

    started, results, errors = threading.Event(), [], []
    original = live.build if pause == "build" else managers_mod.live_manager_records

    def slow(*args, **kwargs):
        value = original(*args, **kwargs)
        if threading.current_thread().name == "old":
            started.set()
            if isinstance(released_after, threading.Event):
                assert released_after.wait(30)
            else:
                time.sleep(released_after)  # the real clock: the drainer's fake one is not this
        return value

    if pause == "build":
        monkeypatch.setattr(live, "build", slow)
    else:
        monkeypatch.setattr(managers_mod, "live_manager_records", slow)

    def run_old(root: Path) -> None:
        try:
            results.append(live.write(root, heartbeat=True))
        except BaseException as exc:
            errors.append(exc)

    return started, results, errors, run_old


@pytest.mark.parametrize(("pause", "tick"), [("managers", 1.0), ("build", 0.1)])
def test_a_slow_writer_with_old_facts_cannot_publish_over_the_question(
    tmp_root: Path, monkeypatch, pause: str, tick: float
):
    """A writer that captured its facts
    before a question published them after it, with a later `observed_at` (and, in the second,
    the same whole-second stamp), and the question vanished from the snapshot. The lock now
    holds from capture to publication, so the question's write waits for the slow one, then
    captures and publishes the newer facts."""
    import threading

    from agent_cli import status

    real_sleep = time.sleep
    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", real_sleep)  # `_real_drain` patched nothing here
    clock[0] = int(clock[0]) + 1.1
    assert status.render_quietly(tmp_root)
    started, results, errors, run_old = _slow_writer(monkeypatch, pause, 0.4)
    thread = threading.Thread(target=run_old, args=(tmp_root,), name="old")
    thread.start()
    assert started.wait(10)
    clock[0] += tick
    assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
    thread.join(10)
    assert not thread.is_alive() and not errors
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    assert snap["needs_you"] and snap["tabs"][0]["label"] == f"✋ {DISPLAY}"
    assert live.write_failed(tmp_root) is None and status.check(tmp_root)["fresh"] is True


def test_a_late_failure_and_an_old_success_never_leave_old_facts_fresh(tmp_root: Path, monkeypatch):
    """A failure recorded late overwrote a newer one,
    and an old success then cleared it, leaving old facts called fresh. In lock order: the old
    success publishes, the question's write publishes the newer facts after it, and a failure
    that lands later still is the last word, so the snapshot is called stale (not fresh with
    old facts) until the next good write."""
    import threading

    from agent_cli import status

    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", time.sleep)
    assert status.render_quietly(tmp_root)
    original_lock = live._publish_lock
    go = threading.Event()

    @contextlib.contextmanager
    def delayed_lock(root, wait=None):
        if threading.current_thread().name == "old-failure":
            assert go.wait(10)
        with original_lock(root, wait):
            yield

    monkeypatch.setattr(live, "_publish_lock", delayed_lock)
    failure = threading.Thread(
        target=lambda: live.record_failure(tmp_root, OSError("an older failure")), name="old-failure"
    )
    failure.start()
    started, results, errors, run_old = _slow_writer(monkeypatch, "build", 0.4)
    success = threading.Thread(target=run_old, args=(tmp_root,), name="old")
    success.start()
    assert started.wait(10)
    clock[0] += 1
    assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
    success.join(10)
    snap = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    assert snap["needs_you"] and live.write_failed(tmp_root) is None  # the newer facts, published
    go.set()
    failure.join(10)
    assert not failure.is_alive() and not success.is_alive() and not errors
    assert live.write_failed(tmp_root) is not None  # the failure landed last, so it stands
    assert status.check(tmp_root)["fresh"] is False
    key = tab_drain.drain_key("manager", SID)
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"
    clock[0] += 1
    assert live.write(tmp_root) is False and live.write_failed(tmp_root) is None  # a good write
    assert status.check(tmp_root)["fresh"] is True


def test_a_heartbeat_that_cannot_have_the_lock_skips_but_a_hook_write_records(tmp_root: Path, monkeypatch):
    """The 5 s heartbeat skips when another writer holds the lock (that writer publishes
    facts at least as fresh); any other write waits, then counts as a failed write, never a
    silent skip. A killed holder releases the lock (its probe, kept as a control)."""
    import subprocess as subprocess_mod
    import sys

    _real_drain(tmp_root, monkeypatch, running=False)
    script = (
        "from pathlib import Path\n"
        "import sys, time\n"
        "from agent_cli.vault import live\n"
        "with live._publish_lock(Path(sys.argv[1])):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(30)\n"
    )
    holder = subprocess_mod.Popen(
        [sys.executable, "-c", script, str(tmp_root)], stdout=subprocess_mod.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(live, "HEARTBEAT_WAIT_S", 0.05)
        monkeypatch.setattr(live, "PUBLISH_WAIT_S", 0.05)
        assert live.write(tmp_root, heartbeat=True) is False and live.write_failed(tmp_root) is None
        live.write_quietly(tmp_root)  # a hook's write
        assert live.write_failed(tmp_root) is not None
    finally:
        holder.kill()
        holder.wait(timeout=5)
    sessions.set_field(tmp_root, SID, "owner_question_pending", True)
    assert live.write(tmp_root, heartbeat=True)
    assert (
        json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["needs_you"]
        and live.write_failed(tmp_root) is None
    )


def _failing_snapshot_writes(root: Path, monkeypatch) -> None:
    """Writes of `live.json` fail where they publish (the real `write`, inside its lock)."""
    original = core.atomic_write

    def fail(path: Path, body: str) -> None:
        if path == live.path(root):
            raise OSError("snapshot temporarily unwritable")
        original(path, body)

    monkeypatch.setattr(core, "atomic_write", fail)


def test_a_failure_recorded_while_a_success_holds_the_lock_survives_it(tmp_root: Path, monkeypatch):
    """A success compared the failure record, then a
    failure was written outside the lock, then the success unlinked it. Failures are now one
    file each, and a success removes only the files it found when it took the lock."""
    from agent_cli import status

    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    assert status.render_quietly(tmp_root)
    live.record_failure(tmp_root, OSError("an earlier failure"))  # found by the success below
    monkeypatch.setattr(live, "PUBLISH_WAIT_S", 0.05)
    # held until the question has failed for the lock: a timer lapsed first under load
    release = threading.Event()
    started, results, errors, run_old = _slow_writer(monkeypatch, "build", release)
    thread = threading.Thread(target=run_old, args=(tmp_root,), name="old")
    thread.start()
    try:
        assert started.wait(10)
        clock[0] += 1
        assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
        folder = tmp_root / live.WRITE_FAILED
        standing = sorted(p.name for p in folder.iterdir() if not p.name.startswith(live.INTENT_PREFIX))
        assert len(standing) == 2  # the earlier one, and the question's, recorded outside the lock
        assert any(p.name.startswith(live.INTENT_PREFIX) for p in folder.iterdir())  # the old write's
    finally:
        release.set()
    thread.join(10)
    # a success, whether it rewrote or found its (old) facts already on disk
    assert not thread.is_alive() and not errors and len(results) == 1
    left = sorted(p.name for p in (tmp_root / live.WRITE_FAILED).iterdir())
    assert len(left) == 1 and left[0] in standing  # the success removed only what it found
    assert live.write_failed(tmp_root) is not None and status.check(tmp_root)["fresh"] is False
    key = tab_drain.drain_key("manager", SID)
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"


@pytest.mark.parametrize("step", [0.1, -60.0])
def test_a_question_shows_whatever_the_clock_while_rewrites_fail(tmp_root: Path, monkeypatch, step: float):
    """A question raised in the snapshot's second,
    or after the clock stepped back a minute, while rewrites failed, lost to the old snapshot,
    because the request's wall-clock time did not exceed `observed_at`. No time orders anything
    now: while rewrites fail, the fallback paints facts it derives itself."""
    from agent_cli import status

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    clock[0] = int(clock[0]) + 0.1
    assert status.render_quietly(tmp_root)
    clock[0] += step
    _failing_snapshot_writes(tmp_root, monkeypatch)
    assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
    assert live.write_failed(tmp_root) is not None
    key = tab_drain.drain_key("manager", SID)
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    tab_drain.run(tmp_root, launches[-1][4])
    assert titles[-1] == f"✋ {DISPLAY}"


@NODE
def test_an_old_plugin_build_is_counted_out_and_reported(tmp_root: Path, tmp_path: Path, monkeypatch, run):
    """A healthy plugin build from before the yield
    protocol held the fallback back past 10 s, then repainted over it. While a snapshot write
    has failed it now counts out, so the fallback paints within 10 s; and since it cannot be
    stopped from repainting, `status check` fails and doctor warns until it is updated."""
    import test_tab_plugin
    from agent_cli import status
    from agent_cli.doctor import checks_state as checks

    body = PLUGIN.read_text(encoding="utf-8")
    protocol = "const TAB_PROTOCOL = 2;"
    failure = "const failure = tabsOff ? { failed: false, error: null } : await this.snapshotFailed(nowMs);"
    assert protocol in body and failure in body, "the plugin source moved; rebuild the old-build baseline"
    body = body.replace(protocol, "const TAB_PROTOCOL = 1;").replace(
        failure, "const failure = { failed: false, error: null };"
    )
    old_plugin = tmp_path / "old-main.js"
    old_plugin.write_text(body, encoding="utf-8")
    monkeypatch.setattr(test_tab_plugin, "PLUGIN", old_plugin)
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=False)
    titles = [f"✅ {DISPLAY}"]
    _plugin_paints(tmp_root, clock, titles)
    assert tab_plugin.active(tmp_root) and tab_plugin.outdated(tmp_root)
    assert status.check(tmp_root)["views"][tab_plugin.HEARTBEAT.as_posix()]["found"] == tab_plugin.OUTDATED
    assert run(["status", "check"]).code != 0
    [row] = checks.plugin_paints(tmp_root)
    assert row["level"] == "warn" and tab_plugin.OUTDATED in row["detail"]
    clock[0] += 1
    asked = clock[0]
    _failing_snapshot_writes(tmp_root, monkeypatch)
    assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
    assert not tab_plugin.active(tmp_root)  # it cannot yield, so it counts out
    first_paint: list[float] = []
    execute = _batch_on(titles)

    def paint(entries, timeout=10):
        first_paint.append(clock[0] - asked)
        return execute(entries, timeout)

    def sleep(seconds: float) -> None:  # the old plugin keeps polling every 2 s
        deadline = clock[0] + seconds
        while clock[0] + 2 < deadline:
            clock[0] += 2
            _plugin_paints(tmp_root, clock, titles)
        clock[0] = deadline
        _plugin_paints(tmp_root, clock, titles)

    monkeypatch.setattr(tab_drain, "_eval", paint)
    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[-1][4])
    assert first_paint and first_paint[0] <= 10
    # the residual: the old build repaints the stale snapshot on its next poll, and says nothing
    clock[0] += 2
    _plugin_paints(tmp_root, clock, titles)
    assert titles == [f"✅ {DISPLAY}"] and run(["status", "check"]).code != 0


def _question_reaches_the_tab(root: Path, monkeypatch, clock, launches, titles) -> None:
    """A question raised while rewrites fail wins over the stale snapshot and is painted."""
    _failing_snapshot_writes(root, monkeypatch)
    assert ops.owner_question(root, session_id=SID, summary="Choose A or B.").code == 0
    assert live.write_failed(root) is not None
    key = tab_drain.drain_key("manager", SID)
    assert tab_drain._desired(root)[key]["label"] == f"✋ {DISPLAY}"
    assert tab_drain._targets(root)[key]["label"] == f"✋ {DISPLAY}"
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert launches
    tab_drain.run(root, launches[-1][4])
    assert titles[-1] == f"✋ {DISPLAY}"


@NODE
@pytest.mark.parametrize("saved_after", [False, True])
def test_while_rewrites_fail_the_fallback_paints_facts_it_derives(
    tmp_root: Path, monkeypatch, saved_after: bool
):
    """With rewrites failing, an older
    saved ✅ request beat the newer question the last good snapshot held, whenever the order
    between them was uncertain. Nothing chooses between a stale snapshot and saved requests now:
    the fallback derives the facts itself (`live.build`, in memory) and paints those, so the
    question shows whichever was saved last. Saved requests only wake the runner."""
    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    key = tab_drain.drain_key("manager", SID)
    if not saved_after:
        tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    raise_question(tmp_root, SID, "manual", "Choose A or B.")  # its hook died before the tab
    clock[0] += 6
    assert live.write(tmp_root, heartbeat=True)
    if saved_after:  # an old ✅ request saved late, after the question's snapshot
        tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    _plugin_paints(tmp_root, clock, titles)
    assert titles == [f"✋ {DISPLAY}"]
    _failing_snapshot_writes(tmp_root, monkeypatch)
    clock[0] += 6
    live.write_quietly(tmp_root, heartbeat=True)
    assert live.write_failed(tmp_root) is not None
    _plugin_paints(tmp_root, clock, titles)  # the plugin yields
    assert not tab_plugin.active(tmp_root)
    assert tab_drain._desired(tmp_root)[key]["label"] == f"✅ {DISPLAY}"
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    tab_drain.run(tmp_root, launches[-1][4])
    assert titles == [f"✋ {DISPLAY}"]


def test_when_deriving_fails_too_every_tab_shows_its_unknown_form(tmp_root: Path, monkeypatch):
    """If the fallback cannot derive the facts either, they are unknown: the old snapshot's
    tabs are presented in their `?` form, and `status check` is not fresh."""
    from agent_cli import status

    _real_drain(tmp_root, monkeypatch, running=False)
    live.record_failure(tmp_root, OSError("a rewrite failed"))

    def broken(root):
        raise OSError("state unreadable")

    monkeypatch.setattr(live, "build", broken)
    key = tab_drain.drain_key("manager", SID)
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✅? {DISPLAY}"
    assert status.check(tmp_root)["fresh"] is False


@NODE
@pytest.mark.parametrize(
    "damage",
    [
        "not_directory",
        pytest.param(
            "permission",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="chmod(0) does not make a directory unlistable on Windows"
            ),
        ),
    ],
)
def test_an_unreadable_failure_record_stands(tmp_root: Path, monkeypatch, damage: str):
    """Python read a failure folder it could not
    list (a file in its place, or no permission) as no failure, so the old snapshot stayed
    authoritative and `status check` fresh while the plugin had yielded. Only a missing folder
    means none stands now: this one stands, unreadable, with its reason."""
    from agent_cli import status

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    assert status.render_quietly(tmp_root)
    failed = tmp_root / live.WRITE_FAILED
    failed.rmdir()  # empty: every write's intent has ended
    if damage == "not_directory":
        failed.write_text("unavailable", encoding="utf-8")
    else:
        failed.mkdir()
        failed.chmod(0)
    try:
        _failing_snapshot_writes(tmp_root, monkeypatch)
        clock[0] += 1
        assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
        assert live.write_failed(tmp_root) is not None and live.failures_unreadable(tmp_root)
        assert tab_drain._snapshot(tmp_root)["authoritative"] is False
        view = status.check(tmp_root)["views"][live.path(tmp_root).relative_to(tmp_root).as_posix()]
        assert view["fresh"] is False and view["found"].startswith("the failure record cannot be read")
        _plugin_paints(tmp_root, clock, titles)
        assert not tab_plugin.active(tmp_root)
        key = tab_drain.drain_key("manager", SID)
        assert tab_drain._targets(tmp_root)[key]["label"] == f"✋ {DISPLAY}"
        monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
        monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
        tab_drain.run(tmp_root, launches[-1][4])
        assert titles == [f"✋ {DISPLAY}"]
    finally:
        if damage == "permission":
            failed.chmod(0o700)


def test_many_standing_failures_are_cleared_by_one_good_write(tmp_root: Path, monkeypatch):
    """2,500 failure files read quickly and one good write clears them all."""
    import uuid

    _real_drain(tmp_root, monkeypatch, running=False)
    live._keep_failure(tmp_root, OSError("snapshot temporarily unwritable"))
    folder = tmp_root / live.WRITE_FAILED
    (kept,) = folder.iterdir()
    for _ in range(2499):  # copies of the real record, without an atomic write each
        (folder / f"{uuid.uuid4().hex}.json").write_bytes(kept.read_bytes())
    if sys.platform == "win32":
        # the first open of each new file is slow on Windows, outside `write_failed`: its first
        # scan took 26 s alone and 46 s under load, then 0.8 to 1.1 s once each file had been
        # opened; open each once so the budget times `write_failed`
        for failure in (tmp_root / live.WRITE_FAILED).iterdir():
            failure.read_bytes()
    started = time.perf_counter()
    assert live.write_failed(tmp_root) is not None
    assert time.perf_counter() - started < 5
    assert live.write(tmp_root) is False and live.write_failed(tmp_root) is None


@NODE
def test_an_unreadable_failure_folder_counts_as_a_failure(tmp_root: Path, monkeypatch, run):
    """The plugin read an unreadable `live-failed/`
    (EACCES) as no failure, stayed active and repainted the stale snapshot. Any error but a
    missing folder now counts as a failure standing: the plugin yields with `read_ok` false, the
    fallback paints within 10 s, and status check and doctor report the read error."""
    from agent_cli import status
    from agent_cli.doctor import checks_state as checks

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    _plugin_paints(tmp_root, clock, titles)
    assert tab_plugin.active(tmp_root)
    clock[0] += 1
    _failing_snapshot_writes(tmp_root, monkeypatch)
    assert ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.").code == 0
    asked = clock[0]
    broken = HARNESS.replace(
        "const painter = new Klass.TabPainter(workspace, vault, '9.9.9');",
        "const fs = require('fs'); const broken = {...fs, promises: {...fs.promises}};"
        "broken.promises.readdir = async (name) => {"
        "if (name.endsWith('live-failed')) {const e = new Error('directory unavailable');"
        "e.code = 'EACCES'; throw e;} return fs.promises.readdir(name);};"
        "const painter = new Klass.TabPainter(workspace, vault, '9.9.9', broken);",
    )

    def poll() -> dict:
        out = _node(
            broken,
            str(PLUGIN),
            str(tmp_root),
            str(int(clock[0] * 1000)),
            json.dumps([{"userTitle": titles[-1]}]),
        )
        result = json.loads(out)
        titles.append(result["titles"][0])
        return result["result"]

    first = poll()
    assert first["yielded"] is True and first["read_ok"] is False
    assert "live-failed" in (first["read_error"] or "") and not tab_plugin.active(tmp_root)
    assert (
        "plugin cannot read live-failed"
        in status.check(tmp_root)["views"][tab_plugin.HEARTBEAT.as_posix()]["found"]
    )
    assert run(["status", "check"]).code != 0
    [row] = checks.plugin_paints(tmp_root)
    assert row["level"] == "warn" and "plugin cannot read live-failed" in row["detail"]
    first_paint: list[float] = []
    execute = tab_drain._eval

    def paint(entries, timeout=10):
        first_paint.append(clock[0] - asked)
        return execute(entries, timeout)

    def sleep(seconds: float) -> None:
        end = clock[0] + seconds
        while clock[0] + 2 < end:
            clock[0] += 2
            poll()
        clock[0] = end
        poll()

    monkeypatch.setattr(tab_drain, "_eval", paint)
    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    tab_drain.run(tmp_root, launches[-1][4])
    assert first_paint and first_paint[0] <= 10 and titles[-1] == f"✋ {DISPLAY}"
    poll()  # the plugin still yields: it does not repaint over the fallback
    assert titles[-1] == f"✋ {DISPLAY}"


def _glitching_session_read(root: Path, monkeypatch, sid: str, glitches: list[int]) -> None:
    """The next `glitches[0]` scans of the session store read `sid`'s record as malformed."""
    session_file = sessions.sessions_dir(root) / f"{sid}.json"
    original_read, original_all = Path.read_text, sessions.all_records
    scanning = [False]

    def read(path, *args, **kwargs):
        if scanning[0] and path == session_file:
            return "{"
        return original_read(path, *args, **kwargs)

    def scan(root_):
        scanning[0] = glitches[0] > 0
        glitches[0] = max(0, glitches[0] - 1)
        try:
            return original_all(root_)
        finally:
            scanning[0] = False

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(sessions, "all_records", scan)


@NODE
def test_a_malformed_record_in_the_fallbacks_derivation_is_unknown_not_idle(tmp_root: Path, monkeypatch):
    """A session record read malformed during the
    fallback's derivation was skipped, so the view lacked the manager and the old ✅ stood as if
    definitive. The derivation is strict now: the facts are unknown, the tab shows `✅?`, and the
    runner stays to read again; once the read recovers, ✋ shows."""
    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    key = tab_drain.drain_key("manager", SID)
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    state = tab_drain._state(tmp_root)
    state["painted"] = {key: {"label": f"✅ {DISPLAY}", "name": DISPLAY, "at": clock[0] - 1}}
    tab_drain._save_state(tmp_root, state)
    raise_question(tmp_root, SID, "manual", "Choose A or B.")
    _failing_snapshot_writes(tmp_root, monkeypatch)
    live.write_quietly(tmp_root)
    _plugin_paints(tmp_root, clock, titles)
    assert not tab_plugin.active(tmp_root)
    glitches = [1]
    _glitching_session_read(tmp_root, monkeypatch, SID, glitches)
    assert tab_drain._targets(tmp_root)[key]["label"] == f"✅? {DISPLAY}"  # unknown, not idle
    seen: list[str] = []
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))

    def sleep(seconds: float) -> None:
        seen.append(titles[-1])
        clock[0] += seconds

    monkeypatch.setattr(tab_drain.time, "sleep", sleep)
    glitches[0] = 1  # the runner's first derivation reads it malformed; the next ones recover
    tab_drain.run(tmp_root, launches[-1][4])
    assert f"✅? {DISPLAY}" in seen and f"✅ {DISPLAY}" not in seen[1:]
    assert titles == [f"✋ {DISPLAY}"]


@NODE
def test_a_persistently_malformed_record_keeps_the_runner_retrying(tmp_root: Path, monkeypatch):
    """With the manager's record malformed,
    the runner crashed on a session lookup instead of painting unknown. It paints `✅?`, keeps
    reading again every UNKNOWN_RETRY_S within its lifetime cap, and never raises."""
    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain, "MAX_LIFETIME_S", 30)
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    raise_question(tmp_root, SID, "manual", "Choose A or B.")
    _failing_snapshot_writes(tmp_root, monkeypatch)
    live.write_quietly(tmp_root)
    _plugin_paints(tmp_root, clock, titles)
    session_file = sessions.sessions_dir(tmp_root) / f"{SID}.json"
    before = session_file.read_text(encoding="utf-8")
    core.atomic_write(session_file, "{")
    try:
        retries: list[float] = []
        monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))

        def sleep(seconds: float) -> None:
            retries.append(seconds)
            clock[0] += seconds

        monkeypatch.setattr(tab_drain.time, "sleep", sleep)
        tab_drain.run(tmp_root, launches[-1][4])
        assert titles == [f"✅? {DISPLAY}"]
        assert retries.count(tab_drain.UNKNOWN_RETRY_S) >= 5  # read again within the cap
        rows = [
            json.loads(line)
            for line in (tab_drain.drain_dir(tmp_root) / "metrics.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert any(row.get("event") == "lifetime-cap" for row in rows)
    finally:
        core.atomic_write(session_file, before)


@NODE
def test_a_slow_read_in_the_fallback_does_not_hold_a_question_back(tmp_root: Path, monkeypatch):
    """The runner held `drainer.lock` while its
    derivation was paused on a slow read, so the question's request waited past the hook's
    budget. The derivation runs outside the lock now: the request returns at once, and when the
    supervisor's next heartbeat finds the runner stalled, a fresh runner paints ✋ within 10 s."""
    import threading

    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    key = tab_drain.drain_key("manager", SID)
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    live._keep_failure(tmp_root, OSError("publication failed"))
    slow_sid = "zzzz1111-2222-3333-4444-555566667777"
    sessions.set_record(tmp_root, slow_sid, pid=11111, name="unrelated ended process")
    slow_file = sessions.sessions_dir(tmp_root) / f"{slow_sid}.json"
    original_read = Path.read_text
    entered, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def read(path, *args, **kwargs):
        # the stalled runner's derivation, in its own thread since then (`tab_drain._derive`)
        if threading.current_thread().name == "tab-derive-slow-fallback" and path == slow_file:
            entered.set()
            assert release.wait(30)
        return original_read(path, *args, **kwargs)

    def derive():
        try:
            tab_drain.run(tmp_root, launches[0][4])
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tab_drain, "DERIVE_STALL_S", 1.0)
    stalled = threading.Thread(target=derive, name="slow-fallback")
    stalled.start()
    try:
        assert entered.wait(5)
        raise_question(tmp_root, SID, "manual", "Choose A or B.")
        asked = time.monotonic()
        out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
        assert time.monotonic() - asked < 2  # not held behind the paused read
        assert (
            out["started"]
            and tab_drain.read_entry(tab_drain.drain_dir(tmp_root) / f"{key}.json")["label"]
            == f"✋ {DISPLAY}"
        )
        tab_drain._LOCK_POLL_SLEEP(1.2)  # real time (`time.sleep` drives the fake clock here)
        live.write_quietly(tmp_root, heartbeat=True)  # the supervisor's heartbeat
        assert len(launches) == 2  # a fresh runner, past the stalled one
        tab_drain.run(tmp_root, launches[-1][4])
        assert titles == [f"✋ {DISPLAY}"] and time.monotonic() - asked < 10
    finally:
        release.set()
        stalled.join(10)
    assert not stalled.is_alive() and not errors
    assert titles == [f"✋ {DISPLAY}"]  # the stalled runner found its token gone and painted nothing


@NODE
def test_a_runner_rechecks_after_a_recovered_publish_and_a_new_failure(tmp_root: Path, monkeypatch):
    """The runner reads the facts again on every pass, across a publish that
    recovered and a new failure."""
    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    live.record_failure(tmp_root, OSError("publication failed"))
    original_eval = _batch_on(titles)
    painted: list[list[str]] = []

    def evaluate(entries, timeout=10):
        if not painted:
            raise_question(tmp_root, SID, "manual", "Choose A or B.")
            clock[0] += 1
            live.write(tmp_root)
            assert live.write_failed(tmp_root) is None
            live._keep_failure(tmp_root, OSError("another publication failed"))
        painted.append([e["label"] for e in entries])
        return original_eval(entries, timeout)

    monkeypatch.setattr(tab_drain, "_eval", evaluate)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    tab_drain.run(tmp_root, launches[-1][4])
    assert titles == [f"✋ {DISPLAY}"]
    assert len(painted) == 2 and tab_drain._state(tmp_root)["runner"] is None
    assert live.write_failed(tmp_root) is not None


@NODE
def test_persisting_failures_rederive_until_the_lifetime_cap(tmp_root: Path, monkeypatch):
    """While failures persist, the runner re-derives on its renewal waits and ends
    at its lifetime cap, recorded."""
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=True)
    monkeypatch.setattr(tab_drain, "MAX_LIFETIME_S", 60)
    live.record_failure(tmp_root, OSError("publication failed"))
    original = live.build
    builds: list[float] = []

    def count(root):
        builds.append(clock[0])
        return original(root)

    monkeypatch.setattr(live, "build", count)
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    started = clock[0]
    tab_drain.run(tmp_root, launches[-1][4])
    rows = [
        json.loads(line)
        for line in (tab_drain.drain_dir(tmp_root) / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row.get("event") == "lifetime-cap" for row in rows)
    assert clock[0] - started < 80 and len(builds) < 100
    assert titles == [f"⏳ {DISPLAY}"]


@pytest.mark.usefixtures("short_lock_waits")
def test_a_request_never_waits_long_for_a_held_drainer_lock(tmp_root: Path, monkeypatch):
    """A request waits for `drainer.lock` at most REQUEST_BUDGET_S in all (its
    turn, then its level check), well inside a hook's time; its own file is saved first, and
    stays unprocessed for the holder's level check when it releases."""
    import threading

    _real_drain(tmp_root, monkeypatch, running=False)
    held, done = threading.Event(), threading.Event()

    def hold() -> None:
        with tab_drain._locked(tmp_root):
            held.set()
            assert done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
        assert time.monotonic() - started < 2 * tab_drain.REQUEST_LOCK_WAIT_S + 1
        assert out == {"started": False, "reason": "busy", "saved": True}
        key = tab_drain.drain_key("manager", SID)
        assert tab_drain.read_entry(tab_drain.drain_dir(tmp_root) / f"{key}.json")["label"] == f"✋ {DISPLAY}"
    finally:
        done.set()
        holder.join(5)


def _paused_runner(root: Path, monkeypatch, launches, pause_at: str):
    """A runner thread that paints ✅, then, on the pass that finds nothing more owed, pauses at
    `pause_at`: "baseline" (reading the request nonces before deriving, under the lock),
    "release" (clearing its token, under the lock, after its decision) or "wake" (its level
    check, after the lock is released)."""
    import threading

    entered, release, errors = threading.Event(), threading.Event(), []
    original_nonces, original_release, original_wake = (
        tab_drain._request_nonces,
        tab_drain._release,
        tab_drain.wake,
    )
    key = tab_drain.drain_key("manager", SID)

    def pause() -> None:
        if threading.current_thread().name == "paused" and not entered.is_set():
            entered.set()
            assert release.wait(20)

    def nonces(root_):
        if pause_at == "baseline" and key in (tab_drain._state(root_).get("painted") or {}):
            pause()  # the exit pass: ✅ is painted already
        return original_nonces(root_)

    def release_(root_, state):
        if pause_at == "release":
            pause()
        original_release(root_, state)

    def wake(root_, wait=tab_drain.REQUEST_LOCK_WAIT_S):
        if pause_at == "wake":
            pause()
        return original_wake(root_, wait)

    def drain():
        try:
            tab_drain.run(root, launches[0][4])
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(tab_drain, "_request_nonces", nonces)
    monkeypatch.setattr(tab_drain, "_release", release_)
    monkeypatch.setattr(tab_drain, "wake", wake)
    thread = threading.Thread(target=drain, name="paused")
    thread.start()
    return thread, entered, release, errors


def _settle(root: Path, launches) -> None:
    """Run the runner a level check started, as its detached process would, until none holds a
    token; then nothing is unprocessed."""
    for _ in range(4):
        token = tab_drain._state(root).get("runner_token")
        if not token:
            break
        assert token in [argv[4] for argv in launches]
        tab_drain.run(root, token)
    state = tab_drain._state(root)
    assert state.get("runner") is None and tab_drain._unprocessed(root, state) == []


def _hand_then_control(root: Path, launches, titles) -> None:
    """The outcome every interleaving must reach, and its control: ✋ painted, no runner left,
    nothing unprocessed; the same request again is "unchanged" and starts nothing (while a
    failure stands a request does not judge, so one runner looks, paints nothing and exits)."""
    _settle(root, launches)
    assert titles == [f"✋ {DISPLAY}"]
    count = len(launches)
    again = tab_drain.request(root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
    if live.write_failed(root) is None:
        assert again == {"started": False, "reason": "unchanged"} and len(launches) == count
    else:
        assert again["started"] and len(launches) == count + 1
        _settle(root, launches)
        assert titles == [f"✋ {DISPLAY}"] and len(launches) == count + 1


@NODE
@pytest.mark.usefixtures("short_lock_waits")
@pytest.mark.parametrize("publication_fails", [False, True])
@pytest.mark.parametrize("pause_at", ["baseline", "release", "wake"])
def test_a_request_saved_during_a_runner_exit_is_never_stranded(
    tmp_root: Path, monkeypatch, pause_at: str, publication_fails: bool
):
    """A request saved while
    the runner was deciding to exit returned "busy", and the runner exited on a decision that
    never read it; the exit re-scan took its baseline after that decision. The wake is now a
    level: the runner records as processed only the nonces it read before deriving the facts it
    decided on, and checks the level after releasing the lock, so at each pause the ✋ request
    is unprocessed for whoever releases last, which starts a runner that paints it."""
    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    if publication_fails:
        live._keep_failure(tmp_root, OSError("publication unavailable"))
    thread, entered, release, errors = _paused_runner(tmp_root, monkeypatch, launches, pause_at)
    try:
        assert entered.wait(5)
        assert titles == [f"✅ {DISPLAY}"]
        raise_question(tmp_root, SID, "manual", "Choose A or B.")
        if not publication_fails:
            live.write(tmp_root)
        started = time.monotonic()
        out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
        assert time.monotonic() - started < tab_drain.REQUEST_BUDGET_S + 0.5
        if pause_at != "wake":  # the paused runner holds the lock and its token
            assert out == {"started": False, "reason": "busy", "saved": True}
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert (sessions.get(tmp_root, SID) or {})["owner_question_pending"] is True
    _hand_then_control(tmp_root, launches, titles)


@NODE
@pytest.mark.usefixtures("short_lock_waits")
@pytest.mark.parametrize("publication_fails", [False, True])
def test_an_owner_question_during_a_runner_exit_is_painted(
    tmp_root: Path, monkeypatch, publication_fails: bool
):
    """`ops.owner_question`
    raises the question, writes the snapshot (or fails to), runs the expiry guard and requests
    ✋ while the runner holds the lock deciding to exit. The hand is painted once it releases."""
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    if publication_fails:
        _failing_snapshot_writes(tmp_root, monkeypatch)
    results: list[dict] = []
    original_request = tab_drain.request

    def request(*args, **kwargs):
        results.append(original_request(*args, **kwargs))
        return results[-1]

    monkeypatch.setattr(tab_drain, "request", request)
    thread, entered, release, errors = _paused_runner(tmp_root, monkeypatch, launches, "release")
    try:
        assert entered.wait(5)
        started = time.monotonic()
        op = ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.")
        assert time.monotonic() - started < 5
        assert op.data["owner_question_pending"] is True
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert results == [{"started": False, "reason": "busy", "saved": True}]
    assert bool(live.write_failed(tmp_root) is not None) == publication_fails
    monkeypatch.setattr(tab_drain, "request", original_request)
    _hand_then_control(tmp_root, launches, titles)


@NODE
@pytest.mark.usefixtures("short_lock_waits")
def test_a_busy_request_behind_an_unchanged_request_is_painted(tmp_root: Path, monkeypatch):
    """The lock's holder was an ordinary request,
    which had chosen the old target, decided "unchanged" and left with no runner. Every lock
    holder now checks the level after releasing: the unchanged request records only its own
    nonce as processed, finds the ✋ request unprocessed and starts a runner."""
    import threading

    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    tab_drain.run(tmp_root, launches[0][4])
    assert tab_drain._state(tmp_root)["runner"] is None
    entered, release = threading.Event(), threading.Event()
    original_targets = tab_drain._targets
    results: list[dict] = []
    errors: list[BaseException] = []

    def targets(root):
        value = original_targets(root)
        if threading.current_thread().name == "unchanged-request":
            entered.set()
            assert release.wait(15)
        return value

    def unchanged_request():
        try:
            results.append(tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}"))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(tab_drain, "_targets", targets)
    holder = threading.Thread(target=unchanged_request, name="unchanged-request")
    holder.start()
    try:
        assert entered.wait(5)
        raise_question(tmp_root, SID, "manual", "Choose A or B.")
        live.write(tmp_root)
        started = time.monotonic()
        out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
        assert time.monotonic() - started < tab_drain.REQUEST_BUDGET_S + 0.5
        assert out == {"started": False, "reason": "busy", "saved": True}
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive() and not errors
    # the holder decided its own request from the old target, then woke a runner for the hand
    assert results == [{"started": False, "reason": "unchanged", "woke": True}]
    assert len(launches) == 2
    monkeypatch.setattr(tab_drain, "_targets", original_targets)
    _hand_then_control(tmp_root, launches, titles)


@NODE
@pytest.mark.parametrize("pause", ["before-read", "after-read"])
@pytest.mark.parametrize("public_op", [False, True])
def test_a_success_between_the_snapshot_reads_never_authorises_older_bytes(
    tmp_root: Path, monkeypatch, public_op: bool, pause: str
):
    """`_snapshot` read `live.json`, then the failure record.
    A concurrent success published ✋ and cleared the failure between the two, so the old ✅
    bytes looked authoritative and the runner marked the question's request processed with ✅
    still shown. The record is now read before the bytes too. A success before the read gives
    the runner its bytes (✋); a success after the read finds the failure standing before it,
    so the runner derives the facts (✋). Through the raw request and through
    `ops.owner_question` with one transient publication failure."""
    import threading

    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    tab_drain.run(tmp_root, launches[0][4])
    assert tab_drain._state(tmp_root)["runner"] is None
    if public_op:
        atomic = core.atomic_write
        fail_write = [True]

        def publish(path, text):
            if path == live.path(tmp_root) and fail_write[0]:
                raise OSError("transient publication failure")
            return atomic(path, text)

        monkeypatch.setattr(core, "atomic_write", publish)
        op = ops.owner_question(tmp_root, session_id=SID, summary="Choose A or B.")
        assert op.data["owner_question_pending"]
        fail_write[0] = False
    else:
        raise_question(tmp_root, SID, "manual", "Choose A or B.")
        live._keep_failure(tmp_root, OSError("transient publication failure"))
        tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
    assert live.write_failed(tmp_root) is not None
    entered, resume = threading.Event(), threading.Event()
    errors: list[BaseException] = []
    calls = [0]
    original_failed = live.write_failed

    def failed(root):
        # the runner's first `_snapshot` reads the record, then the bytes, then the record
        if threading.current_thread().name == "runner" and not entered.is_set():
            calls[0] += 1
            if calls[0] == (1 if pause == "before-read" else 2):
                entered.set()
                assert resume.wait(10)
        return original_failed(root)

    def runner():
        try:
            tab_drain.run(tmp_root, launches[-1][4])
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(live, "write_failed", failed)
    thread = threading.Thread(target=runner, name="runner")
    thread.start()
    try:
        assert entered.wait(5)
        live.write_quietly(tmp_root)  # a concurrent hook's success publishes ✋, clears the failure
        assert original_failed(tmp_root) is None
        assert (
            json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"][0]["label"] == f"✋ {DISPLAY}"
        )
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert (sessions.get(tmp_root, SID) or {})["owner_question_pending"] is True
    assert titles == [f"✋ {DISPLAY}"]
    state = tab_drain._state(tmp_root)
    assert state["runner"] is None and tab_drain._unprocessed(tmp_root, state) == []
    # control: nothing is owed afterwards, so the guard starts nothing
    count = len(launches)
    assert not tab_drain.ensure_expiry_guard(tmp_root) and len(launches) == count


def test_status_judges_the_snapshot_bytes_it_read(tmp_root: Path, monkeypatch):
    """A failure standing before `live.json` is read keeps those
    bytes from counting as current, even when a success clears it before the check after."""
    from agent_cli import status

    _real_drain(tmp_root, monkeypatch, running=True)
    assert status._live_state(tmp_root) == "current"
    live._keep_failure(tmp_root, OSError("transient publication failure"))
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if path == live.path(tmp_root):  # a success clears the record right after this read
            live._clear_failures(live._failures(tmp_root))
        return text

    monkeypatch.setattr(Path, "read_text", read)
    assert status._live_state(tmp_root) == "a later rewrite failed"
    assert live.write_failed(tmp_root) is None  # cleared between the reads, as in the race
    assert status._live_state(tmp_root) == "current"  # the next read judges the new bytes


@NODE
def test_a_request_never_derives_and_returns_at_once(tmp_root: Path, monkeypatch):
    """A request derived the facts before saving
    itself, so a stalled read held the hook. Only runners derive now: a request saves its file,
    ensures a runner and returns, and the runner paints ✋."""
    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    tab_drain.run(tmp_root, launches[0][4])
    raise_question(tmp_root, SID, "manual", "Choose A or B.")
    live._keep_failure(tmp_root, OSError("publication failed"))
    original_build = live.build
    derived_by: list[str] = []

    def build(root):
        import threading

        derived_by.append(threading.current_thread().name)
        return original_build(root)

    monkeypatch.setattr(live, "build", build)
    original_read = Path.read_text
    session_file = sessions.sessions_dir(tmp_root) / f"{SID}.json"

    def read(path, *args, **kwargs):  # a session read would stall: the request must not make one
        assert path != session_file, "a request read a session record"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    started = time.monotonic()
    out = tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✋ {DISPLAY}")
    assert time.monotonic() - started < 1 and out["started"] and not derived_by
    monkeypatch.setattr(Path, "read_text", original_read)
    tab_drain.run(tmp_root, launches[-1][4])
    assert derived_by and titles == [f"✋ {DISPLAY}"]


@NODE
def test_a_session_folder_that_cannot_be_listed_leaves_the_facts_unknown(tmp_root: Path, monkeypatch):
    """The strict scan used `Path.glob`, which hides
    a listing error, so an unlistable session folder read as empty and the old ✅ stood. Strict
    scans list with `iterdir`, which raises: the facts are unknown, the tab shows `✅?`, a runner
    is started, and ✋ shows once the folder can be listed again."""
    from agent_cli.hooks.attention import raise_question

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    key = tab_drain.drain_key("manager", SID)
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, f"✅ {DISPLAY}")
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    tab_drain.run(tmp_root, launches[0][4])
    raise_question(tmp_root, SID, "manual", "Choose A or B.")
    live._keep_failure(tmp_root, OSError("publication failed"))
    _plugin_paints(tmp_root, clock, titles)
    folder = sessions.sessions_dir(tmp_root)
    before = folder.stat().st_mode
    folder.chmod(0o111)
    try:
        try:
            list(folder.iterdir())
        except PermissionError:
            pass
        else:
            pytest.skip("this OS/user bypasses directory read permissions")
        with pytest.raises(PermissionError), core.strict_reads():
            sessions.all_records(tmp_root)
        assert tab_drain._view_now(tmp_root).get("unknown") is True
        assert tab_drain._targets(tmp_root)[key]["label"] == f"✅? {DISPLAY}"
        assert tab_drain.ensure_expiry_guard(tmp_root) is True and len(launches) == 2
        retries = [0]

        def sleep(seconds: float) -> None:  # the folder can be listed again after one retry
            retries[0] += 1
            clock[0] += seconds
            if retries[0] == 1:
                folder.chmod(before)

        monkeypatch.setattr(tab_drain.time, "sleep", sleep)
        tab_drain.run(tmp_root, launches[-1][4])
        assert titles == [f"✋ {DISPLAY}"]
    finally:
        folder.chmod(before)


KILLED_WRITER = r"""
import json, sys
from pathlib import Path
from agent_cli import sessions
from agent_cli.hooks.run import run
from agent_cli.ops import owner_question
from agent_cli.vault import live, managers

root, sid, route, manager_pid, *real = sys.argv[1:]
root = Path(root)
managers.claude_live_sessions = lambda: {}
sessions.resolve_identity = lambda: (int(manager_pid), True)
if not real:  # the test runs the watchdog itself; with `real` the detached one is started
    live._watch = lambda root, intent: print('watch ' + intent.name, flush=True)
else:  # intents recorded 9 s ago, so the detached watchdog, on the real clock, waits 2 s, not 11
    from datetime import timedelta
    from agent_cli import core
    _now = core.now
    core.now = lambda: _now() - timedelta(seconds=9)
original_build = live.build
def paused_build(root):
    print('capture-ready', flush=True)
    sys.stdin.read(1)
    return original_build(root)
live.build = paused_build
if route == 'manual':
    owner_question(root, session_id=sid, summary='Choose A or B.')
elif route == 'sync-hook':
    run(root, 'pre-ask', json.dumps(
        {'session_id': sid, 'hook_event_name': 'PreToolUse', 'tool_name': 'AskUserQuestion'}))
elif route.startswith('worker:'):  # the supervisor's save path: a worker returns a question
    from agent_cli import core
    from agent_cli.dispatch.state import load, save
    d = load(root, route.removeprefix('worker:'))
    save(root, {**d, 'state': 'question', 'finished': core.iso(core.now())})
    live.write_quietly(root)
elif route == 'codex':
    run(root, 'codex-activity', json.dumps(
        {'session_id': sid, 'hook_event_name': 'PreToolUse', 'tool_name': 'request_user_input'}))
else:
    run(root, 'activity', json.dumps(
        {'session_id': sid, 'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash'}))
"""


def _paused_writer(root: Path, route: str, *, real: bool = False):
    """A child running `route`'s real op or hook, paused inside `live.build`: (process, the
    intents it started watchdogs for).

    The child's lines are read by a thread into a queue, waited on with a timeout (Codex P1
    review): `select` on a buffered text reader missed a line it had already
    buffered. The thread stops at `capture-ready`, so `_kill`'s `communicate` reads the rest."""
    import queue
    import threading

    argv = [sys.executable, "-c", KILLED_WRITER, str(root), SID, route, str(os.getpid())]
    proc = subprocess.Popen(
        [*argv, *(["real"] if real else [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.put(raw.strip())
            if raw.strip() == "capture-ready":
                return
        lines.put("")  # EOF: the child ended first

    threading.Thread(target=read, daemon=True).start()
    watched: list[str] = []
    while True:
        try:
            line = lines.get(timeout=15)
        except queue.Empty:
            proc.kill()
            raise AssertionError("the child never reached the snapshot build") from None
        if line == "capture-ready":
            return proc, watched
        if not line.startswith("watch "):
            proc.kill()
            raise AssertionError(f"unexpected child output {line!r}: {proc.communicate()[1]}")
        watched.append(line.removeprefix("watch "))


def _newest_intent(root: Path, clock: list[float]) -> float:
    """The fake clock, or the newest intent's stamp if later. A child stamps its intents by the
    real clock, which runs ahead of the fake one by however long the child took to start: over
    a second under load."""
    stamps = [
        json.loads(p.read_text(encoding="utf-8"))["at"]
        for p in (root / live.WRITE_FAILED).iterdir()
        if p.name.startswith(live.INTENT_PREFIX)
    ]
    return max([clock[0], *stamps])


def _past_intent_stale(root: Path, clock: list[float]) -> None:
    clock[0] = _newest_intent(root, clock) + live.INTENT_STALE_S + 1


def _kill(proc) -> None:
    proc.kill()
    # gone before `communicate` closes its stdin: Windows terminates asynchronously, and a child
    # still running at that EOF resumed its paused build and published
    proc.wait(timeout=10)
    _, err = proc.communicate(timeout=10)
    assert proc.returncode == (1 if sys.platform == "win32" else -9), err


@NODE
@pytest.mark.parametrize("route", ["manual", "sync-hook"])
def test_a_question_writer_killed_mid_publication_still_reaches_the_tab(
    tmp_root: Path, monkeypatch, route: str
):
    """A question writer killed inside `live.build` (a hook
    past its client's timeout) ran no handler and recorded no failure, so the old ✅ stayed
    authoritative: the plugin painted it, the guard started nothing and status was fresh.
    Writes are pessimistic now. The question path records an intent before the fact, and each
    write records its own; a kill leaves them standing. The snapshot is not authoritative,
    the plugin paints through for INTENT_STALE_S (a write in flight) and then yields, status is
    not fresh, and the question's watchdog publishes ✋ once the writer is gone."""
    clock, launches, _ = _real_drain(tmp_root, monkeypatch, running=False)
    sessions.set_field(tmp_root, SID, "pid", os.getpid())
    assert status.render_quietly(tmp_root)
    assert status.check(tmp_root)["fresh"]
    assert _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0])["result"]["read_ok"]
    proc, watched = _paused_writer(tmp_root, route)
    try:
        assert (sessions.get(tmp_root, SID) or {})["owner_question_pending"] is True
    finally:
        _kill(proc)

    # the intents stand: the old ✅ is not authoritative, and the fallback's view is the hand
    assert watched and live.write_failed(tmp_root) is not None and live.failures_only(tmp_root) is None
    assert tab_drain._snapshot(tmp_root)["authoritative"] is False
    assert json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"][0]["label"] == f"✅ {DISPLAY}"
    assert tab_drain._view(tmp_root)["tabs"][tab_drain.drain_key("manager", SID)]["label"] == f"✋ {DISPLAY}"

    # one real plugin instance, fifty polls over 100 simulated seconds: it paints through while
    # the intents are young (a write in flight), then yields
    clock[0] = _newest_intent(tmp_root, clock)
    prefix = HARNESS[: HARNESS.index("const result = await painter.poll")]
    js = (
        prefix
        + r"""
const polls = [];
for (let i = 1; i <= 50; i++) {
  const result = await painter.poll(Number(now) + i * 2000);
  polls.push({yielded: !!result.yielded, title: leaves[0].view.state.userTitle});
}
console.log(JSON.stringify(polls));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    )
    out = _node(
        js,
        str(PLUGIN),
        str(tmp_root),
        str(int(clock[0] * 1000)),
        json.dumps([{"userTitle": f"✅ {DISPLAY}"}]),
    )
    polls = json.loads(out)
    first = next(i for i, poll in enumerate(polls) if poll["yielded"])
    assert 3 <= first <= 7 and all(poll["yielded"] for poll in polls[first:])
    assert all(poll["title"] == f"✅ {DISPLAY}" for poll in polls)
    clock[0] += 100
    assert not tab_plugin.active(tmp_root)  # yielded: the fallback is the painter
    assert status._live_state(tmp_root).startswith("a snapshot write did not finish")
    assert status.check(tmp_root)["fresh"] is False

    # the question's watchdog: its writer is gone, so it publishes; the plugin then paints ✋
    assert live.watch_intent(tmp_root, watched[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    assert live.write_failed(tmp_root) is None
    fixed = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0])
    assert fixed["titles"] == [f"✋ {DISPLAY}"] and not fixed["result"]["yielded"]
    assert status.check(tmp_root)["fresh"] is True


def test_a_write_in_flight_keeps_status_fresh_and_the_fallback_off_the_question_mark(
    tmp_root: Path, monkeypatch
):
    """A young intent (a write in flight) is not a failure for `status`, and the
    fallback derives the facts while it stands instead of painting `?`. An old one whose writer
    still holds `PUBLISH_LOCK` is still in flight; once the lock is free it is a writer that
    died. A success ends its own intent and abandoned ones, never a live writer's."""
    from agent_cli.hooks.attention import raise_question

    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    assert status.render_quietly(tmp_root)
    raise_question(tmp_root, SID, "manual", "Choose A or B.")  # its intent stands: not yet written
    [intent] = list((tmp_root / live.WRITE_FAILED).iterdir())
    assert status._live_state(tmp_root) == "current" and not live.yield_due(tmp_root)
    view = tab_drain._view(tmp_root)
    assert view.get("derived") and not view.get("unknown")
    assert view["tabs"][tab_drain.drain_key("manager", SID)]["label"] == f"✋ {DISPLAY}"
    clock[0] += live.INTENT_STALE_S + 1
    with live._publish_lock(tmp_root):
        assert status._live_state(tmp_root) == "current"  # old, but its write holds the lock
        clock[0] += live.INTENT_RUNNING_S - live.INTENT_STALE_S
        # past INTENT_RUNNING_S a write holding the lock has stalled
        assert status._live_state(tmp_root).startswith("a snapshot write has been running")
    assert status._live_state(tmp_root).startswith("a snapshot write did not finish")
    assert live.yield_due(tmp_root)
    # another live process's intent survives a success; this thread's own ends with it
    other = tmp_root / live.WRITE_FAILED / f"{live.INTENT_PREFIX}other.json"
    other.write_text(json.dumps({"at": clock[0], "intent": True, "pid": 4242}), encoding="utf-8")
    live.write_quietly(tmp_root)
    assert not intent.exists() and other.exists()
    other.write_text(json.dumps({"at": clock[0], "intent": True, "pid": 5555}), encoding="utf-8")
    live.write_quietly(tmp_root)  # its writer is gone: abandoned, so ended
    assert not other.exists() and status._live_state(tmp_root) == "current"


@NODE
@pytest.mark.parametrize("before", ["idle", "working"])
def test_a_permission_writer_killed_mid_publication_is_watched(tmp_root: Path, monkeypatch, before: str):
    """A permission hook killed inside `live.build`
    had persisted `waiting_on_you`, an owner approval, under unwatched intents only, so the
    plugin yielded with the old ✅ or ⚙️ showing and nothing republished. A session write that
    changes the owner obligation (`live.owner_obligation`) is watched now, whatever wrote it."""
    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    sessions.set_field(tmp_root, SID, "pid", os.getpid())
    sessions.set_field(tmp_root, SID, "activity", {"state": before, "at": core.iso(core.now())})
    old = f"{'✅' if before == 'idle' else '⚙️'} {DISPLAY}"
    assert status.render_quietly(tmp_root)
    assert _paint(tmp_root, [{"userTitle": old}], now=clock[0])["result"]["read_ok"]
    proc, watched = _paused_writer(tmp_root, "permission")
    _kill(proc)
    assert (sessions.get(tmp_root, SID) or {})["activity"]["state"] == "waiting_on_you"
    assert live.build(tmp_root)["needs_you"][0]["reason"] == "approval"
    assert len(watched) == 1 and not tab_drain._snapshot(tmp_root)["authoritative"]
    _past_intent_stale(tmp_root, clock)
    assert status._live_state(tmp_root).startswith("a snapshot write did not finish")
    assert live.watch_intent(tmp_root, watched[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    assert _paint(tmp_root, [{"userTitle": old}], now=clock[0])["titles"] == [f"✋ {DISPLAY}"]
    assert status.check(tmp_root)["fresh"] is True


@NODE
@pytest.mark.parametrize("route", ["manual", "codex"])
def test_a_stalled_question_writer_is_painted_around_and_followed_until_it_dies(
    tmp_root: Path, monkeypatch, route: str
):
    """A question writer stalled inside `live.build`
    outlived its watchdog's six looks, and `status` called the old ✅ current because the
    stalled writer held `PUBLISH_LOCK`. The watchdog now follows its intent until it is gone
    (to `WATCH_MAX_S`, pausing 1 s doubling to 60 s). While the writer lives, a stale intent
    starts the fallback, which derives ✋ without that lock and paints it as the plugin yields;
    past `INTENT_RUNNING_S` status reports the write as running. When the writer dies, the
    watchdog publishes ✋."""
    real_alive = core.is_alive
    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (4242, 99999, os.getpid()) or real_alive(pid))
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    monkeypatch.setattr(tab_drain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    sessions.set_field(tmp_root, SID, "pid", os.getpid())
    assert status.render_quietly(tmp_root)
    assert _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0])["result"]["read_ok"]
    proc, watched = _paused_writer(tmp_root, route)
    seen: dict[str, object] = {}
    try:
        assert (sessions.get(tmp_root, SID) or {})["owner_question_pending"] and len(watched) == 1
        want = live.build(tmp_root)["tabs"][0]["label"]  # the facts: the hand (a Codex tab reads cx)
        assert want.startswith("✋ ")
        start = _newest_intent(tmp_root, clock)

        def sleep(seconds: float) -> None:
            clock[0] += seconds
            if proc.poll() is not None:
                return
            if launches and "fallback" not in seen:
                # the plugin, polled after the intent went stale, has yielded; the fallback
                # the watchdog started paints the derived hand while the writer still stalls
                assert _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0])["result"]["yielded"]
                tab_drain.run(tmp_root, launches[-1][4])
                seen["fallback"] = list(titles)
            if clock[0] - start > live.INTENT_RUNNING_S + 1:
                seen["status"] = status._live_state(tmp_root)
                seen["fresh"] = status.check(tmp_root)["fresh"]
                _kill(proc)  # the writer dies at last

        assert live.watch_intent(tmp_root, watched[0], sleep=sleep) is True
    finally:
        if proc.poll() is None:
            _kill(proc)
    assert seen["fallback"] == [want]
    assert str(seen["status"]).startswith("a snapshot write has been running") and seen["fresh"] is False
    # published once the writer died: the plugin paints the hand and status is fresh again
    assert live.write_failed(tmp_root) is None
    assert json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"][0]["label"] == want
    assert _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0])["titles"] == [want]
    assert status.check(tmp_root)["fresh"] is True


def test_the_watchdog_follows_its_intent_to_its_cap_and_no_further(tmp_root: Path, monkeypatch):
    """A watchdog looks with backoff (1 s doubling to `WATCH_BACKOFF_MAX_S`) until its
    intent is `WATCH_MAX_S` old, starting the fallback on each look while the writer lives."""
    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    intent = tmp_root / live.WRITE_FAILED / f"{live.INTENT_PREFIX}stalled.json"
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text(json.dumps({"at": clock[0], "intent": True, "pid": 4242}), encoding="utf-8")
    pauses: list[float] = []
    takeovers: list[int] = []
    monkeypatch.setattr(tab_drain, "on_snapshot_failure", lambda root: takeovers.append(1))

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        clock[0] += seconds

    assert live.watch_intent(tmp_root, intent.name, sleep=sleep) is False
    assert (
        intent.exists() and clock[0] - json.loads(intent.read_text(encoding="utf-8"))["at"] > live.WATCH_MAX_S
    )
    backoff = pauses[1:]
    assert backoff[:7] == [1, 2, 4, 8, 16, 32, 60] and max(backoff) == live.WATCH_BACKOFF_MAX_S
    assert len(takeovers) == len(backoff) < 80


@NODE
def test_a_worker_question_writer_killed_mid_publication_is_watched(tmp_root: Path, monkeypatch):
    """The worker side of the same finding: a dispatch write that changes what the dispatch owes
    (`live.dispatch_obligation`; here a worker returning a question, which the supervisor
    saves through `dispatch.state.save` and then publishes) records a watched intent first.
    Its writer SIGKILLed inside `live.build`, the watchdog publishes the question: the
    owner's `needs_you` row, and the tabs the facts give (the watch tab's ✋)."""
    from test_dispatch_notice_hooks import DID

    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=True)
    sessions.set_field(tmp_root, SID, "pid", os.getpid())
    assert status.render_quietly(tmp_root)
    before = {
        t["key"]: t["label"] for t in json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"]
    }
    proc, watched = _paused_writer(tmp_root, f"worker:{DID}")
    _kill(proc)
    assert len(watched) == 1 and not tab_drain._snapshot(tmp_root)["authoritative"]
    derived = live.build(tmp_root)
    want = {t["key"]: t["label"] for t in derived["tabs"]}
    assert [n.get("id") for n in derived["needs_you"] if n["kind"] == "worker"] == [DID]
    watch = tab_drain.drain_key("watch", DID)
    assert want[watch].startswith("✋ ") and want != before
    published = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    assert not published["needs_you"]  # the old bytes: no question yet
    _past_intent_stale(tmp_root, clock)
    assert status._live_state(tmp_root).startswith("a snapshot write did not finish")
    assert live.watch_intent(tmp_root, watched[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    published = json.loads(live.path(tmp_root).read_text(encoding="utf-8"))
    assert [n.get("id") for n in published["needs_you"] if n["kind"] == "worker"] == [DID]
    assert {t["key"]: t["label"] for t in published["tabs"]} == want
    assert live.write_failed(tmp_root) is None and status.check(tmp_root)["fresh"] is True
    # a write that leaves the obligation as it is records no watched intent
    from agent_cli.dispatch.state import load, save

    save(tmp_root, {**load(tmp_root, DID), "note": "unrelated"})
    assert not list((tmp_root / live.WRITE_FAILED).iterdir())


@NODE
def test_the_detached_watchdog_publishes_a_killed_question(tmp_root: Path, monkeypatch):
    """A control, with the real detached watchdog process (no test stand-in): a question
    writer SIGKILLed inside `live.build` is published by it once the intent is stale."""
    clock, _, _ = _real_drain(tmp_root, monkeypatch, running=False)
    sessions.set_field(tmp_root, SID, "pid", os.getpid())
    assert status.render_quietly(tmp_root)
    proc, _ = _paused_writer(tmp_root, "manual", real=True)
    _kill(proc)
    assert (sessions.get(tmp_root, SID) or {})["owner_question_pending"]
    assert not tab_drain._snapshot(tmp_root)["authoritative"]
    deadline = time.monotonic() + 20
    while json.loads(live.path(tmp_root).read_text(encoding="utf-8"))["tabs"][0]["label"] != f"✋ {DISPLAY}":
        assert time.monotonic() < deadline, "the detached watchdog never published the question"
        time.sleep(0.1)
    assert live.write_failed(tmp_root) is None
    out = _paint(tmp_root, [{"userTitle": f"✅ {DISPLAY}"}], now=clock[0] + 15)
    assert out["titles"] == [f"✋ {DISPLAY}"]


def _until(check, timeout: float, what: str) -> float:
    """Wait (real time) until `check()` holds; the seconds it took."""
    started = time.monotonic()
    while not check():
        assert time.monotonic() - started < timeout, what
        threading.Event().wait(0.05)  # not `time.sleep`, which a test may have replaced
    return time.monotonic() - started


@NODE
def test_a_slow_fact_store_shows_unknown_then_the_hand(tmp_root: Path, monkeypatch):
    """A permission hook's publication stalled on a
    slow fact store, and the watchdog's takeover derived the facts first, through the same
    store, so it stalled too: no runner, the plugin yielded and ✅ stayed. Triggers derive
    nothing now; they start a runner. The runner's derivation runs in one thread with a
    deadline (`DERIVE_DEADLINE_S`): past it the facts are unknown, so it paints `✅?`, and
    while the read stays stuck no second derivation starts. Once the store answers, ✋."""
    import threading

    from agent_cli import features
    from agent_cli.hooks.run import run as run_hook

    clock, launches, titles = _real_drain(tmp_root, monkeypatch, running=False)
    # the real deadline and stall, scaled down from 5 s and 7 s
    monkeypatch.setattr(tab_drain, "DERIVE_DEADLINE_S", 1.0)
    monkeypatch.setattr(tab_drain, "DERIVE_STALL_S", 3.0)
    sessions.set_field(tmp_root, SID, "pid", os.getpid())
    assert status.render_quietly(tmp_root)
    watched: list[str] = []
    monkeypatch.setattr(live, "_watch", lambda root, intent: watched.append(intent.name))
    monkeypatch.setattr(sessions, "resolve_identity", lambda: (os.getpid(), True))
    monkeypatch.setattr(tab_drain, "_eval", _batch_on(titles))
    original = features.load_all
    release, stop = threading.Event(), threading.Event()
    readers: list[str] = []
    errors: list[BaseException] = []
    hook: list[tuple[str, int]] = []

    def slow_store(root):
        name = threading.current_thread().name
        readers.append(name)
        if name != "MainThread":
            assert release.wait(60), "the test did not release the slow fact store"
        return original(root)

    monkeypatch.setattr(features, "load_all", slow_store)

    def guarded(target):
        def body() -> None:
            try:
                target()
            except BaseException as exc:
                errors.append(exc)

        return body

    def writer() -> None:
        hook.append(
            run_hook(tmp_root, "activity", json.dumps(
                {"session_id": SID, "hook_event_name": "PermissionRequest", "tool_name": "Bash"}))
        )  # fmt: skip

    def drain_sleep(seconds: float) -> None:
        clock[0] += seconds
        threading.Event().wait(0.05)

    writing = threading.Thread(target=guarded(writer), name="writer")
    writing.start()
    watching = runner = None
    try:
        _until(lambda: "writer" in readers, 5, "the writer never reached the fact store")
        assert (sessions.get(tmp_root, SID) or {})["activity"]["state"] == "waiting_on_you"
        assert len(watched) == 1
        clock[0] += live.INTENT_RUNNING_S + 1
        assert status._live_state(tmp_root).startswith("a snapshot write has been running")
        # the watchdog's look starts a runner at once, deriving nothing
        watching = threading.Thread(
            target=guarded(lambda: live.watch_intent(tmp_root, watched[0], sleep=lambda s: stop.wait(30))),
            name="watchdog",
        )
        watching.start()
        _until(lambda: bool(launches), 3, "the watchdog started no runner")
        assert "watchdog" not in readers
        assert _paint(tmp_root, [{"userTitle": titles[0]}], now=clock[0])["result"]["yielded"]
        monkeypatch.setattr(tab_drain.time, "sleep", drain_sleep)
        runner = threading.Thread(
            target=guarded(lambda: tab_drain.run(tmp_root, launches[-1][4])), name="runner"
        )
        runner.start()
        took = _until(lambda: titles == [f"✅? {DISPLAY}"], 12, "the runner never painted the unknown form")
        assert took < tab_drain.DERIVE_DEADLINE_S + 3
        # the read stays stuck across retries: one derivation, not one per pass
        threading.Event().wait(tab_drain.DERIVE_DEADLINE_S + 1)
        derives = [t for t in threading.enumerate() if t.name == "tab-derive-runner"]
        assert len(derives) == 1 and readers.count("tab-derive-runner") == 1
        assert titles == [f"✅? {DISPLAY}"]
    finally:
        release.set()
    try:
        _until(lambda: titles == [f"✋ {DISPLAY}"], 15, "the hand was never painted")
    finally:
        stop.set()
        for thread in (writing, watching, runner):
            if thread is not None:
                thread.join(30)
    assert not errors and len(hook) == 1 and hook[0][1] == 0
    assert live.build(tmp_root)["needs_you"][0]["reason"] == "approval"
