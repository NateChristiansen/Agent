"""The Dashboard's "Clear leases" link: the agent-terminal-focus plugin
handles `obsidian://agent-lease-clear`, runs the venv's `agent lease clear` with no shell, and
asks first only when the CLI says the session was active recently."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"

# Stubs `obsidian` and `child_process`. The fake CLI answers needs_confirm on the first call for
# session "busy", and clears on every other call.
HARNESS = r"""
const Module = require('module');
const [pluginPath, platform, session, answer] = process.argv.slice(1);
Object.defineProperty(process, 'platform', {value: platform});
const calls = [], notices = [], modals = [];
let handler = null;
class Plugin {
  constructor(app) { this.app = app; }
  addRibbonIcon() {} registerEvent() {} registerDomEvent() {} register() {} registerInterval() {}
  registerObsidianProtocolHandler(name, cb) { handler = {name, cb}; }
}
class Modal {
  constructor(app) { this.app = app; this.contentEl = el(); }
  open() { modals.push(this); this.onOpen(); }
  close() {}
}
function el() {
  const node = {texts: [], buttons: [], empty() {},
    createEl(tag, opts) { const child = el(); child.text = opts?.text; node.texts.push(opts?.text); if (tag === 'button') node.buttons.push(child); return child; },
    createDiv() { return node; }};
  return node;
}
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') return {addIcon() {}, Modal, Notice: class { constructor(m) { notices.push(m); } }, Plugin};
  if (request === 'child_process') return {execFile(cmd, args, opts, cb) {
    calls.push({cmd, args, shell: opts.shell || false});
    if (session === 'unknown') {  // the CLI's --json AgentError shape, with a nonzero exit
      const failure = Object.assign(new Error('Command failed'), {code: 2});
      setImmediate(() => cb(failure, JSON.stringify({error: "no session or lease holder named 'unknown'", code: 2}), ''));
      return;
    }
    const confirmNeeded = session === 'busy' && !args.includes('--yes');
    const data = confirmNeeded
      ? {needs_confirm: true, recent_minutes: 3, areas: ['vault:src'], cleared: []}
      : {needs_confirm: false, areas: ['vault:src'], cleared: ['vault:src'], kept: []};
    setImmediate(() => cb(null, JSON.stringify(data), ''));
  }};
  return load.call(this, request, ...rest);
};
globalThis.window = {setInterval() { return 1; }};
globalThis.document = {};
const app = {workspace: {getLeavesOfType: () => [], onLayoutReady() {}, on() { return {}; }},
             vault: {adapter: {getBasePath: () => platform === 'win32' ? 'C:\\vault' : '/vault'}}};
const plugin = new (require(pluginPath))(app);
plugin.onload();
(async () => {
  await handler.cb({session});
  if (modals.length && answer === 'yes') {
    modals[0].contentEl.buttons[0].onclick();
    await new Promise((r) => setTimeout(r, 20));
  }
  await new Promise((r) => setTimeout(r, 20));
  console.log(JSON.stringify({name: handler.name, calls, notices,
    modal: modals.map((m) => m.contentEl.texts.filter(Boolean))}));
})();
"""


def _run(platform: str, session: str, answer: str = "no") -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(PLUGIN), platform, session, answer],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_a_quiet_session_clears_in_one_click(platform: str):
    out = _run(platform, "codex-1")
    assert out["name"] == "agent-lease-clear"
    assert out["modal"] == []
    [call] = out["calls"]
    # Windows runs the venv's python, never the agent.exe shim.
    expected = "C:\\vault\\.venv\\Scripts\\python.exe" if platform == "win32" else "/vault/.venv/bin/agent"
    assert call["cmd"].replace("/", "\\") == expected.replace("/", "\\") and call["shell"] is False
    module = ["-I", "-m", "agent_cli"] if platform == "win32" else []
    assert call["args"][: len(module) + 1] == [*module, "--root"]
    assert call["args"][-3:] == ["lease", "clear", "codex-1"]
    assert out["notices"] == ["Cleared vault:src"]


def test_a_recent_session_asks_first_then_clears_on_yes():
    out = _run("darwin", "busy", "yes")
    assert out["modal"] and "3m ago" in " ".join(out["modal"][0])
    assert [c["args"][-1] for c in out["calls"]] == ["busy", "--yes"]
    assert out["notices"] == ["Cleared vault:src"]


def test_a_recent_session_is_left_alone_on_cancel():
    out = _run("darwin", "busy", "no")
    assert len(out["calls"]) == 1 and out["notices"] == []


def test_a_cli_error_becomes_a_notice():
    out = _run("darwin", "unknown")
    assert out["notices"] == ["Clear leases failed: no session or lease holder named 'unknown'"]


def test_a_bad_session_id_never_reaches_the_cli():
    out = _run("darwin", "x; rm -rf /")
    assert out["calls"] == [] and out["notices"] == ["Clear leases: not a session id"]
