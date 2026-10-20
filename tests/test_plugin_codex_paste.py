"""Right-click in a Codex manager tab on Windows pasted twice: Codex's TUI pastes
on the right mouse button itself, and the Terminal plugin's copyPaste action pasted again; a
long paste even submitted between the two. agent-terminal-focus stops a no-selection right-click
before the plugin's handler in ribbon Codex tabs only, and lets a selection through to be
copied."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"

# Builds leaves from the plugin's own ribbon profiles (or a plain shell), runs the guard on each,
# then fires a right-click at each guarded container with and without a selection.
HARNESS = r"""
const Module = require('module');
const [pluginPath, platform] = process.argv.slice(1);
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') return {addIcon() {}, Modal: class {}, Notice: class {}, Plugin: class {}};
  if (request === 'child_process') return {execFile() { return {pid: 1}; }};
  return load.call(this, request, ...rest);
};
const Klass = require(pluginPath);
const UUID = '0f8e2c4a-1b2c-4d3e-8f90-123456789abc';
function leafFor(kind) {
  let profile;
  if (kind === 'plain') {
    profile = {executable: 'powershell.exe', args: ['-NoLogo']};
  } else {
    Object.defineProperty(process, 'platform', {value: 'win32'});
    profile = Klass.managerProfile(kind, `${kind}:${UUID}`, 'C:\\vault');
  }
  Object.defineProperty(process, 'platform', {value: platform});
  let listeners = [];
  let selected = false;
  const containerEl = {
    addEventListener(type, fn, opts) { listeners.push({type, fn, capture: !!opts?.capture}); },
    removeEventListener(type, fn) { listeners = listeners.filter((l) => l.type !== type || l.fn !== fn); },
  };
  const leaf = {
    getViewState: () => ({type: 'terminal:terminal', state: {'terminal:terminal': {profile}}}),
    view: {containerEl, emulator: {terminal: {hasSelection: () => selected}}},
  };
  // onXterm: the click landed on the terminal surface, or elsewhere in the view
  const click = (withSelection, onXterm = true) => {
    selected = withSelection;
    const target = {closest: (sel) => (sel === '.xterm' && onXterm ? {} : null)};
    const ev = {target, prevented: false, stopped: false,
                preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
    listeners.filter((l) => l.type === 'contextmenu').forEach((l) => l.fn(ev));
    return {prevented: ev.prevented, stopped: ev.stopped};
  };
  return {leaf, count: () => listeners.length, capture: () => listeners.map((l) => l.capture), click};
}
const out = {};
for (const kind of ['codex', 'claude', 'plain']) {
  const {leaf, count, capture, click} = leafFor(kind);
  // one plugin load: its own guarded set and disposer list
  let disposers = [];
  let guarded = new Set();
  const first = Klass.guardCodexRightClick(leaf, guarded, (d) => disposers.push(d));
  const second = Klass.guardCodexRightClick(leaf, guarded, (d) => disposers.push(d));
  const result = {guarded: first, again: second, capture: capture(),
                  noSelection: click(false), selection: click(true), offSurface: click(false, false)};
  // unload, then a fresh load: one listener again, never two
  disposers.forEach((d) => d());
  result.afterUnload = count();
  disposers = [];
  guarded = new Set();
  Klass.guardCodexRightClick(leaf, guarded, (d) => disposers.push(d));
  result.afterReload = count();
  out[kind] = result;
}
console.log(JSON.stringify(out));
"""


def _run(platform: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(PLUGIN), platform], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_codex_tab_right_click_without_selection_skips_the_plugin_paste():
    codex = _run("win32")["codex"]
    assert codex["guarded"] is True and codex["capture"] == [True]
    assert codex["noSelection"] == {"prevented": True, "stopped": True}


def test_a_selection_in_a_codex_tab_still_reaches_the_plugin_copy():
    assert _run("win32")["codex"]["selection"] == {"prevented": False, "stopped": False}


def test_the_guard_is_added_once_per_tab():
    codex = _run("win32")["codex"]
    assert codex["again"] is False and len(codex["capture"]) == 1


def test_a_right_click_off_the_terminal_surface_is_left_alone():
    assert _run("win32")["codex"]["offSurface"] == {"prevented": False, "stopped": False}


def test_unload_removes_the_listener_and_a_reload_adds_exactly_one():
    codex = _run("win32")["codex"]
    assert codex["afterUnload"] == 0 and codex["afterReload"] == 1


@pytest.mark.parametrize("kind", ["claude", "plain"])
def test_claude_and_plain_tabs_are_untouched(kind: str):
    tab = _run("win32")[kind]
    assert tab["guarded"] is False and tab["capture"] == []


def test_other_platforms_are_untouched():
    assert _run("darwin")["codex"]["guarded"] is False
