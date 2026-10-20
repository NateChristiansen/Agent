import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import symlinks
from agent_cli import obsidian


def _seed_vault(tmp_root: Path):
    odir = tmp_root / ".obsidian"
    odir.mkdir()
    (odir / "community-plugins.json").write_text('["dataview", "terminal"]')
    (odir / "app.json").write_text('{"spellcheck": false}')
    (odir / "core-plugins.json").write_text('{"file-explorer": true, "bases": false}')
    (odir / "templates.json").write_text('{"dateFormat": "YYYY-MM-DD"}')
    plugins = odir / "plugins"
    for pid in ("terminal", "dataview"):
        (plugins / pid).mkdir(parents=True)
        (plugins / pid / "manifest.json").write_text(f'{{"id": "{pid}"}}')
    (plugins / "obsidian-shellcommands").mkdir()
    (plugins / "obsidian-shellcommands" / "manifest.json").write_text('{"id": "obsidian-shellcommands"}')
    (plugins / "obsidian-shellcommands" / "data.json").write_text(
        json.dumps(
            {
                "shell_commands": [
                    {"id": "user-thing", "alias": "Mine"},
                    {"id": "agent-log", "alias": "stale"},
                ],
                "settings_version": "0.23.0",
            }
        )
    )
    snippet_src = tmp_root / "harness/obsidian/snippets"
    snippet_src.mkdir(parents=True)
    (snippet_src / "agent-callouts.css").write_text(".callout {}")
    (snippet_src / "agent-terminal-tabs.css").write_text(".workspace-tab-header { --tab-width: 300px; }")
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "harness/obsidian/plugins",
        tmp_root / "harness/obsidian/plugins",
    )


def test_setup_merges_without_deleting(tmp_root: Path, run):
    _seed_vault(tmp_root)
    r = run(["--json", "obsidian", "setup"])
    assert r.code == 0
    odir = tmp_root / ".obsidian"
    plugins = json.loads((odir / "community-plugins.json").read_text(encoding="utf-8"))
    assert plugins[:2] == ["dataview", "terminal"]
    assert "obsidian-shellcommands" in plugins and "homepage" in plugins
    assert "show-hidden-files" not in plugins
    for pid in obsidian.LOCAL_PLUGINS:
        assert plugins.count(pid) == 1
        for name in ("manifest.json", "main.js"):
            source = tmp_root / "harness/obsidian/plugins" / pid / name
            installed = odir / "plugins" / pid / name
            assert installed.read_bytes() == source.read_bytes()
    app = json.loads((odir / "app.json").read_text(encoding="utf-8"))
    assert app["spellcheck"] is False
    assert app["alwaysUpdateLinks"] is False
    # into the linked notes repo, never beside a note in the harness checkout
    assert app["attachmentFolderPath"] == "notes/attachments"
    assert app["newFileFolderPath"] == "notes"
    assert "repos/" in app["userIgnoreFilters"]
    assert ".ruff_cache/" in app["userIgnoreFilters"]
    core = json.loads((odir / "core-plugins.json").read_text(encoding="utf-8"))
    assert core["file-explorer"] is True and core["bases"] is True and core["daily-notes"] is True
    assert core["templates"] is True
    daily = json.loads((odir / "daily-notes.json").read_text(encoding="utf-8"))
    assert daily == {
        "folder": "journal",
        "format": "YYYY/MM-MMMM/DD-dddd",
        "template": "harness/obsidian/templates/daily",
        "autorun": False,
    }
    assert (tmp_root / "journal").is_dir()
    templates = json.loads((odir / "templates.json").read_text(encoding="utf-8"))
    assert templates == {"dateFormat": "YYYY-MM-DD", "folder": "harness/obsidian/templates"}
    appearance = json.loads((odir / "appearance.json").read_text(encoding="utf-8"))
    assert appearance["enabledCssSnippets"] == ["agent-callouts", "agent-terminal-tabs"]
    assert (odir / "snippets/agent-callouts.css").read_text(encoding="utf-8") == ".callout {}"
    assert "--tab-width: 300px" in (odir / "snippets/agent-terminal-tabs.css").read_text(encoding="utf-8")
    assert (tmp_root / "notes/.gitkeep").exists()
    assert not (tmp_root / "harness/attachments").exists()
    assert not (odir / "app.json").read_text(encoding="utf-8").endswith("\n")
    missing = " ".join(r.json["missing_plugins"])
    assert "homepage" in missing and "terminal" not in missing and "obsidian-shellcommands" not in missing
    # setup no longer configures any plugin's data.json (harness/obsidian.md has the manual
    # steps). A pre-existing one is left exactly as it was, and none gets created.
    sc = json.loads((odir / "plugins/obsidian-shellcommands/data.json").read_text(encoding="utf-8"))
    assert sc == {
        "shell_commands": [{"id": "user-thing", "alias": "Mine"}, {"id": "agent-log", "alias": "stale"}],
        "settings_version": "0.23.0",
    }
    assert not (odir / "plugins/homepage/data.json").exists()
    assert not (odir / "plugins/cmdr/data.json").exists()
    assert not (odir / "plugins/vscode-editor/data.json").exists()
    assert not (odir / "plugins/obsidian-git/data.json").exists()
    assert not (odir / "hotkeys.json").exists()
    # the one plugin setting setup owns: Dataview's JavaScript queries, which the Dashboard's
    # live views need
    assert json.loads((odir / "plugins/dataview/data.json").read_text(encoding="utf-8")) == {
        "enableDataviewJs": True,
        "enableInlineDataviewJs": False,
    }


def test_setup_turns_dataview_js_on_and_keeps_the_rest(tmp_root: Path, run):
    _seed_vault(tmp_root)
    data = tmp_root / ".obsidian/plugins/dataview/data.json"
    data.write_text(json.dumps({"refreshInterval": 5000, "enableDataviewJs": False}))
    run(["obsidian", "setup"])
    assert json.loads(data.read_text(encoding="utf-8")) == {
        "refreshInterval": 5000,
        "enableDataviewJs": True,
        "enableInlineDataviewJs": False,
    }


def test_setup_leaves_existing_plugin_data_untouched(tmp_root: Path, run):
    """setup() stopped configuring plugin data.json (see harness/obsidian.md). Whatever is
    already there, however it got there, is left byte-for-byte alone."""
    _seed_vault(tmp_root)
    p = tmp_root / ".obsidian/plugins/vscode-editor"
    p.mkdir(parents=True)
    seed = json.dumps({"extensions": ["ts", "Md", ".rb"], "fontSize": 16, "minimap": True})
    (p / "data.json").write_text(seed)
    run(["obsidian", "setup"])
    assert (p / "data.json").read_text(encoding="utf-8") == seed


def test_setup_is_idempotent(tmp_root: Path, run):
    _seed_vault(tmp_root)
    run(["obsidian", "setup"])
    snapshot = {p: p.read_text(encoding="utf-8") for p in (tmp_root / ".obsidian").rglob("*.json")}
    run(["obsidian", "setup"])
    for p, text in snapshot.items():
        assert p.read_text(encoding="utf-8") == text, p
    plugins = json.loads((tmp_root / ".obsidian/community-plugins.json").read_text(encoding="utf-8"))
    assert plugins.count("agent-terminal-focus") == 1


def test_local_plugin_updates_only_changed_files(tmp_root: Path, run):
    _seed_vault(tmp_root)
    run(["obsidian", "setup"])
    target = tmp_root / ".obsidian/plugins/agent-terminal-focus"
    before = {name: (target / name).stat().st_mtime_ns for name in ("main.js", "manifest.json")}
    run(["obsidian", "setup"])
    assert {name: (target / name).stat().st_mtime_ns for name in before} == before
    source = tmp_root / "harness/obsidian/plugins/agent-terminal-focus/main.js"
    source.write_text(source.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
    run(["obsidian", "setup"])
    assert (target / "main.js").read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert (target / "manifest.json").stat().st_mtime_ns == before["manifest.json"]


def test_setup_from_empty_vault(tmp_root: Path, run):
    r = run(["--json", "obsidian", "setup"])
    assert r.code == 0
    assert (tmp_root / ".obsidian/community-plugins.json").exists()
    assert len(r.json["missing_plugins"]) == len(obsidian.RECOMMENDED_PLUGINS)


@pytest.mark.parametrize(("stdout", "running"), [('"Obsidian.exe","4242"', True), ("INFO: No tasks", False)])
def test_app_running_asks_tasklist_on_windows(monkeypatch, stdout: str, running: bool):
    """Windows has no pgrep, so this answered no there."""
    seen: list[list[str]] = []

    def fake(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(obsidian.sys, "platform", "win32")
    monkeypatch.setattr(obsidian.subprocess, "run", fake)
    assert obsidian.app_running() is running
    assert seen[0][0] == "tasklist"


def test_cli_wrapper_is_fail_open(tmp_root: Path, notes_remote: Path, run, monkeypatch):
    which = shutil.which
    monkeypatch.setattr(obsidian.shutil, "which", lambda c, **k: None if c == "obsidian" else which(c, **k))
    monkeypatch.delenv("OBSIDIAN_CLI", raising=False)
    monkeypatch.setattr(obsidian, "CLI_FALLBACKS", ())  # nor a developer's real install
    r = run(["--json", "obsidian", "open", "features/001-x/plan.md"])
    assert r.code == 0
    assert "not on PATH" in r.json["skipped"]
    r = run(["obsidian", "daily"])
    assert r.code == 0
    # auto-open never breaks feature creation
    assert run(["feature", "new", "x", "--title", "X"]).code == 0


@pytest.mark.skipif(sys.platform == "win32", reason="the fake obsidian binary is a POSIX shell script")
def test_cli_wrapper_calls_binary(tmp_root: Path, notes_remote: Path, run, monkeypatch):
    fake = tmp_root / "fakebin"
    fake.mkdir()
    exe = fake / "obsidian"
    exe.write_text('#!/bin/sh\necho "$@" > "$0.log"\n')
    exe.chmod(0o755)
    monkeypatch.setenv("OBSIDIAN_CLI", str(exe))
    monkeypatch.setattr(obsidian, "app_running", lambda: True)
    r = run(["--json", "obsidian", "open", "features/001-x/plan.md"])
    assert r.code == 0 and r.json["ok"] is True
    assert (fake / "obsidian.log").read_text(encoding="utf-8").strip() == "open file=features/001-x/plan"
    monkeypatch.setattr(obsidian, "app_running", lambda: False)  # explicit launch: test_obsidian_run.py
    (fake / "obsidian.log").unlink()
    run(["feature", "new", "y", "--title", "Y"])
    assert not (fake / "obsidian.log").exists()  # auto-open stays quiet when the app is down


def test_install_plugins_is_off_under_the_test_clock(tmp_root: Path):
    """The same guard the notifier and the tab relabel use. Nothing in the suite reaches GitHub,
    and every existing `setup` test stays offline for free."""
    rows = obsidian.install_plugins(tmp_root)
    assert {r["state"] for r in rows} == {"skipped"}
    assert len(rows) == len(obsidian.PLUGIN_SOURCES)


def test_install_plugins_is_idempotent_by_pinned_version(tmp_root: Path, monkeypatch):
    """The owner's condition for dropping the vendored builds: deterministic and idempotent.

    A plugin already at its pinned version is not downloaded again, so a second `setup` touches
    the network only for what is missing or off-version.
    """
    monkeypatch.delenv("AGENT_NOW", raising=False)
    calls: list[str] = []

    def fake_fetch(url: str, timeout: float = 30.0) -> bytes | None:
        calls.append(url)
        if url.endswith("manifest.json"):
            return b'{"id": "dataview", "version": "0.5.68"}'
        return b"// build"

    monkeypatch.setattr(obsidian, "_fetch", fake_fetch)

    first = obsidian.install_plugins(tmp_root, ["dataview"])
    assert first == [{"plugin": "dataview", "state": "installed", "version": "0.5.68"}]
    assert calls, "the first run must download"

    calls.clear()
    second = obsidian.install_plugins(tmp_root, ["dataview"])
    assert second == [{"plugin": "dataview", "state": "already", "version": "0.5.68"}]
    assert calls == [], "an installed plugin at the pinned version must not be fetched again"


def test_install_plugins_tolerates_a_missing_styles_and_survives_a_failure(tmp_root: Path, monkeypatch):
    """`show-hidden-files` ships no styles.css, so a 404 on it is not a failure. A real download
    failure leaves the plugin absent and reports a row, because an offline clone still finishes."""
    monkeypatch.delenv("AGENT_NOW", raising=False)

    def fake_fetch(url: str, timeout: float = 30.0) -> bytes | None:
        if url.endswith("styles.css"):
            return None
        if url.endswith("manifest.json"):
            return b'{"id": "show-hidden-files", "version": "1.0.3"}'
        return b"// build"

    monkeypatch.setattr(obsidian, "_fetch", fake_fetch)
    (row,) = obsidian.install_plugins(tmp_root, ["show-hidden-files"])
    assert row["state"] == "installed"
    target = tmp_root / ".obsidian/plugins/show-hidden-files"
    assert (target / "main.js").exists() and not (target / "styles.css").exists()

    def broken_fetch(url: str, timeout: float = 30.0) -> bytes | None:
        raise OSError("no network")

    monkeypatch.setattr(obsidian, "_fetch", broken_fetch)
    (failed,) = obsidian.install_plugins(tmp_root, ["cmdr"])
    assert failed["state"] == "failed" and "no network" in failed["detail"]
    assert not (tmp_root / ".obsidian/plugins/cmdr/main.js").exists()


def test_every_recommended_plugin_has_a_pinned_source():
    """The enabled list and the download list cannot drift: an id enabled with no source would
    be an empty plugin directory a fresh clone never fills."""
    assert set(obsidian.RECOMMENDED_PLUGINS) | set(obsidian.DISABLED_PLUGINS) == set(obsidian.PLUGIN_SOURCES)
    assert not set(obsidian.RECOMMENDED_PLUGINS) & set(obsidian.DISABLED_PLUGINS)


# ------------------------------------------------------------------ Every plugin works


def test_seeds_fill_only_missing_config(tmp_root: Path, run):
    """Nothing under .obsidian/ is tracked; a fresh clone gets the curated config from the
    seeds, and a file the owner or Obsidian already wrote is never overwritten."""
    seeds = tmp_root / obsidian.CONFIG_SEED_SRC
    (seeds / "plugins" / "homepage").mkdir(parents=True)
    (seeds / "plugins" / "homepage" / "data.json").write_text(
        '{"defaultNote": "Dashboard"}', encoding="utf-8"
    )
    (seeds / "hotkeys.json").write_text('{"seed": true}', encoding="utf-8")
    run(["obsidian", "setup"])
    odir = tmp_root / ".obsidian"
    assert json.loads((odir / "plugins/homepage/data.json").read_text(encoding="utf-8")) == {
        "defaultNote": "Dashboard"
    }
    (odir / "hotkeys.json").write_text('{"mine": true}', encoding="utf-8")
    run(["obsidian", "setup"])
    assert json.loads((odir / "hotkeys.json").read_text(encoding="utf-8")) == {"mine": True}


def test_terminal_gets_dom_and_the_harness_profiles_first(tmp_root: Path, run):
    from agent_cli import obsidian_config

    data = tmp_root / ".obsidian" / obsidian_config.TERMINAL_DATA
    data.parent.mkdir(parents=True)
    data.write_text(
        json.dumps(
            {"preferredRenderer": "webgl", "pinNewInstance": True, "profiles": {"mine": {"type": "external"}}}
        ),
        encoding="utf-8",
    )
    run(["obsidian", "setup"])
    written = json.loads(data.read_text(encoding="utf-8"))
    assert written["preferredRenderer"] == "dom", "webgl corrupted glyphs"
    assert written["pinNewInstance"] is True, "other settings are kept"
    assert list(written["profiles"])[:3] == ["agent-macos", "agent-linux", "agent-windows"]
    assert "mine" in written["profiles"], "the owner's own profiles are kept, after ours"
    windows = written["profiles"]["agent-windows"]
    assert windows["executable"] == "powershell.exe" and windows["platforms"] == {"win32": True}
    assert Path(windows["pythonExecutable"]).parts[-4:] == (
        ".agent",
        "terminal-python",
        "Scripts",
        "python.exe",
    )
    before = data.read_text(encoding="utf-8")
    run(["obsidian", "setup"])
    assert data.read_text(encoding="utf-8") == before, "idempotent"


def test_shell_commands_get_a_windows_variant(tmp_root: Path, run):
    from agent_cli import obsidian_config

    data = tmp_root / ".obsidian" / obsidian_config.SHELLCOMMANDS_DATA
    data.parent.mkdir(parents=True)
    commands = [
        {"id": "agent-doctor", "platform_specific_commands": {"default": "bin/agent doctor"}},
        {"id": "mine", "platform_specific_commands": {"default": "echo hi"}},
    ]
    data.write_text(json.dumps({"shell_commands": commands}), encoding="utf-8")
    run(["obsidian", "setup"])
    written = json.loads(data.read_text(encoding="utf-8"))["shell_commands"]
    assert written[0]["platform_specific_commands"] == {
        "default": "bin/agent doctor",
        "win32": "bin\\agent.cmd doctor",
    }
    assert written[1]["platform_specific_commands"] == {"default": "echo hi"}, "not ours: untouched"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runs the harness plugin")
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_file_links_reject_network_paths_before_filesystem_access(platform: str):
    """A rejected file host or Windows network path never reaches a filesystem API."""
    script = r"""
const assert = require('assert/strict');
const Module = require('module');
const [pluginPath, platform] = process.argv.slice(1);
Object.defineProperty(process, 'platform', {value: platform});
const path = require('path')[platform === 'win32' ? 'win32' : 'posix'];
const root = platform === 'win32' ? 'C:\\vault' : '/vault';
let vaultRoot = root, decodedFile = path.join(root, 'plan.md'), conversions = 0;
const fsCalls = [], lookups = [], opened = [];
const file = {path: 'plan.md'};
const load = Module._load;
Module._load = function(request, ...args) {
  if (request === 'obsidian') return {Plugin: class {}, Modal: class {}, MarkdownRenderChild: class {}};
  if (request === 'path') return path;
  if (request === 'url') return {fileURLToPath() { conversions++; return decodedFile; }};
  if (request === 'fs') return {
    realpathSync(file) { fsCalls.push('realpath'); return file; },
    existsSync() { fsCalls.push('exists'); return true; },
    readdirSync() { fsCalls.push('readdir'); return []; },
  };
  if (request === 'electron') return {shell: {openExternal() { opened.push('external'); }}};
  return load.call(this, request, ...args);
};
const {linkHandler} = require(pluginPath);
const app = {
  vault: {
    adapter: {getBasePath: () => vaultRoot},
    getAbstractFileByPath(relative) { lookups.push(relative); return file; },
  },
  workspace: {getLeaf(newLeaf) { return {openFile(f) { opened.push([f, newLeaf]); }}; }},
};
const handler = linkHandler('Agent', app);
function reject(uri) {
  fsCalls.length = lookups.length = opened.length = 0;
  handler.activate({}, uri);
  assert.deepEqual(fsCalls, [], 'rejected path reached the filesystem');
  assert.deepEqual(lookups, [], 'rejected path reached the vault');
  assert.deepEqual(opened, [], 'rejected path opened');
}
reject('file://server/share/plan.md');
assert.equal(conversions, 0, 'reject remote hosts before fileURLToPath');
const localURI = platform === 'win32' ? 'file:///C:/vault/plan.md' : 'file:///vault/plan.md';
if (platform === 'win32') {
  for (const networkPath of ['\\\\server\\share\\plan.md', '//server/share/plan.md']) {
    decodedFile = networkPath;
    reject(localURI);
    decodedFile = path.join(root, 'plan.md');
    vaultRoot = networkPath;
    reject(localURI);
    vaultRoot = root;
  }
}
handler.activate({}, localURI.replace('file:///', 'file://localhost/'));
assert.deepEqual(lookups, ['plan.md'], 'localhost remains a local file URL');
assert.deepEqual(opened, [[file, false]], 'open the exact vault file');
assert.equal(fsCalls.length, 2);
console.log(JSON.stringify({checked: platform === 'win32' ? 6 : 2}));
"""
    plugin = Path(__file__).resolve().parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"
    result = subprocess.run(["node", "-e", script, str(plugin), platform], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"checked": 6 if platform == "win32" else 2}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runs the harness plugin")
def test_file_links_through_top_level_vault_symlinks(tmp_path: Path):
    """Resolved notes paths open through their vault entries, without admitting siblings."""
    symlinks.require()
    root = tmp_path / "vault"
    root.mkdir()
    notes = tmp_path / "notes"
    features = notes / "features"
    features.mkdir(parents=True)
    (features / "a draft ü.md").write_text("plan")
    (features / "plan#draft.md").write_text("draft")
    (notes / "outside.md").write_text("outside")
    (notes / "features-other").mkdir()
    (notes / "features-other/plan.md").write_text("outside")
    (notes / "agent.yaml").write_text("config")
    (root / "features").symlink_to(features, target_is_directory=True)
    (root / "agent.yaml").symlink_to(notes / "agent.yaml")
    (root / "broken").symlink_to(notes / "absent", target_is_directory=True)
    (root / "plain.md").write_text("plain")
    script = r"""
const assert = require('assert/strict');
const Module = require('module');
const fs = require('fs');
const path = require('path');
const {pathToFileURL} = require('url');
const [pluginPath, root, notes] = process.argv.slice(1);
const load = Module._load;
Module._load = function(request, ...args) {
  if (request === 'obsidian') return {Plugin: class {}, Modal: class {}, MarkdownRenderChild: class {}};
  if (request === 'electron') return {shell: {openExternal() { assert.fail('file opened externally'); }}};
  return load.call(this, request, ...args);
};
const {linkHandler} = require(pluginPath);
const opened = [], created = [];
const app = {
  vault: {
    adapter: {getBasePath: () => root},
    getAbstractFileByPath(relative) {
      return fs.existsSync(path.join(root, relative)) ? {path: relative} : null;
    },
  },
  workspace: {
    openLinkText(relative) { created.push(relative); },
    getLeaf(newLeaf) { return {openFile(file) { opened.push([file.path, newLeaf]); }}; },
  },
};
const readdir = fs.readdirSync;
let scans = 0;
fs.readdirSync = function(...args) { scans++; return readdir.apply(this, args); };
let checked = 0;
function check(handler, uri, expected, event = {}) {
  opened.length = 0;
  handler.activate(event, uri);
  assert.deepEqual(opened, expected === null ? [] : [[expected, !!(event.metaKey || event.ctrlKey)]], 'file open');
  assert.deepEqual(created, [], 'file links must not create notes through openLinkText');
  checked++;
}
const uri = (file) => pathToFileURL(file).href;
const handler = linkHandler('Agent', app);
const realFile = uri(path.join(notes, 'features/a draft ü.md'));
check(handler, realFile, 'features/a draft ü.md');
assert.equal(scans, 1);
check(handler, realFile, 'features/a draft ü.md', {metaKey: true});
check(handler, realFile, 'features/a draft ü.md', {ctrlKey: true});
check(handler, uri(path.join(notes, 'agent.yaml')), 'agent.yaml');
assert.equal(scans, 1, 'hits reuse the entry map and broken entries do not hide valid ones');
check(handler, uri(path.join(root, 'features/a draft ü.md')), 'features/a draft ü.md');
check(handler, uri(path.join(root, 'plain.md')), 'plain.md');
check(handler, uri(path.join(root, 'plain.md')).replace('file:///', 'file://localhost/'), 'plain.md');
// Audit 10 §A2, round 3 R2 retires openLinkText's missing-note creation.
check(handler, uri(path.join(root, 'features/missing.md')), null);
assert.equal(fs.existsSync(path.join(notes, 'features/missing.md')), false);
check(handler, uri(path.join(root, 'features/plan#draft.md')), 'features/plan#draft.md');
check(handler, uri(path.join(notes, 'features/plan#draft.md')), 'features/plan#draft.md');
check(handler, uri(path.join(notes, 'features/missing.md')), null);
check(handler, uri(path.join(notes, 'outside.md')), null);
check(handler, uri(path.join(notes, 'features-other/plan.md')), null);
check(handler, `${uri(path.join(notes, 'features'))}/../outside.md`, null);
check(handler, `${uri(root)}/../notes/outside.md`, null);
check(handler, `${uri(root)}/%2e%2e/notes/outside.md`, null);
check(handler, uri(root), null);
// A link added after the first lookup is picked up on a miss.
fs.mkdirSync(path.join(notes, 'journal'));
fs.writeFileSync(path.join(notes, 'journal/today.md'), 'today');
fs.symlinkSync(path.join(notes, 'journal'), path.join(root, 'journal'), 'dir');
const beforeRefresh = scans;
check(handler, uri(path.join(notes, 'journal/today.md')), 'journal/today.md');
assert.equal(scans, beforeRefresh + 1);
check(handler, uri(path.join(notes, 'journal/today.md')), 'journal/today.md');
assert.equal(scans, beforeRefresh + 1);
// Each handler owns its cache.
check(linkHandler('Agent', app), realFile, 'features/a draft ü.md');
assert.equal(scans, beforeRefresh + 2);
console.log(JSON.stringify({checked}));
"""
    plugin = Path(__file__).resolve().parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"
    result = subprocess.run(
        ["node", "-e", script, str(plugin), str(root), str(notes)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"checked": 20}


# Loads the harness plugin under Node with `obsidian` and `electron` mocked, then drives it the
# way Obsidian does. Terminal 3.27.2 (pinned) exposes the xterm instance as
# `leaf.view.emulator.terminal` (`get emulator()` on its view, `this.terminal=new Terminal(...)`
# in its emulator), and xterm reads `options.linkHandler` each time it resolves a link.
_PLUGIN_HARNESS = r"""
const Module = require('module');
const [pluginPath, platform] = process.argv.slice(1);
Object.defineProperty(process, 'platform', {value: platform});
const opened = [], fileOpened = [], linkOpened = [], windowOpened = [], notices = [], intervals = [], layoutChange = [];
const path = require('path');
const {pathToFileURL} = require('url');
let layoutReady = null, viewState = null;
class Plugin {
  constructor(app) { this.app = app; this.ribbons = []; }
  addRibbonIcon(icon, title, cb) { this.ribbons.push({title, cb}); }
  registerEvent() {} registerDomEvent() {} register() {} registerInterval() {}
  registerObsidianProtocolHandler() {}
}
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') return {addIcon() {}, Modal: class {}, Notice: class { constructor(m) { notices.push(m); } }, Plugin};
  if (request === 'electron') return {shell: {openExternal(url) { opened.push(url); }}};
  // the Windows orphan sweep (#70) finds no processes here; tests/test_plugin_orphan_sweep.py covers it
  if (request === 'child_process') return {execFile(cmd, args, opts, cb) { setImmediate(() => cb(null, '[]', '')); return {pid: 1}; }};
  return load.call(this, request, ...rest);
};
globalThis.window = {setInterval(fn) { intervals.push(fn); return intervals.length; }, open(url) { windowOpened.push(url); }};
globalThis.document = {};
const xterm = () => ({options: {linkHandler: null}});
const restored = {view: {emulator: {terminal: xterm()}}};
const slow = {view: {}};
const leaves = [restored, slow];
const created = [];
let failNext = false;
const app = {
  workspace: {
    openLinkText(...args) { linkOpened.push(args); },
    getLeavesOfType: (type) => (type === 'terminal:terminal' ? leaves : []),
    onLayoutReady(cb) { const prev = layoutReady; layoutReady = () => { if (prev) prev(); cb(); }; },
    on(name, cb) { if (name === 'layout-change') layoutChange.push(cb); return {}; },
    getLeaf(newLeaf) {
      if (typeof newLeaf === 'boolean') return {openFile(file) { fileOpened.push([file.path, newLeaf]); }};
      const leaf = {
        pinned: false,
        detached: false,
        async setViewState(state) {
          if (failNext) { failNext = false; throw new Error('no terminal'); }
          viewState = state;
          this.view = {getViewType: () => 'terminal:terminal', state: {userTitle: state.state['terminal:terminal'].userTitle}};
        },
        setPinned(value) { this.pinned = value; },
        detach() { this.detached = true; },
      };
      created.push(leaf);
      return leaf;
    },
  },
  vault: {
    getName: () => 'My Vault', adapter: {getBasePath: () => 'C:\\vault'},
    getAbstractFileByPath(relative) {
      return ['features/a draft ü.md', 'features/meta.md', 'features/ctrl.md',
              'features/normal.md', 'inside.md'].includes(relative) ? {path: relative} : null;
    },
  },
};
const plugin = new (require(pluginPath))(app);
plugin.onload();
layoutReady();
const out = {restored: restored.view.emulator.terminal.options.linkHandler !== null};
slow.view.emulator = {terminal: xterm()};
out.slowBeforeSweep = slow.view.emulator.terminal.options.linkHandler !== null;
intervals.forEach((fn) => fn());
out.slowAfterSweep = slow.view.emulator.terminal.options.linkHandler !== null;
const fresh = {view: {emulator: {terminal: xterm()}}};
leaves.push(fresh);
(async () => {
layoutChange.forEach((fn) => fn());
out.freshBeforeDebounce = fresh.view.emulator.terminal.options.linkHandler !== null;
await new Promise((resolve) => setTimeout(resolve, 275));
out.fresh = fresh.view.emulator.terminal.options.linkHandler !== null;
const handler = fresh.view.emulator.terminal.options.linkHandler;
out.allowNonHttpProtocols = handler.allowNonHttpProtocols;
for (const uri of ['https://auth.openai.com/oauth?x=1', 'http://localhost:1455/cb', 'file:///etc/passwd',
                   'vscode://file/x', 'not a url',
                   'obsidian://open?vault=My%20Vault&file=features%2F001-x%2Forders%2F001-mac-01.md',
                   'obsidian://open?vault=Other&file=x', 'obsidian://other?vault=My%20Vault&file=x',
                   'obsidian://open?vault=My%20Vault', 'obsidian://open?vault=My%20Vault&file=x&file=y']) handler.activate({}, uri);
handler.activate({}, ['java','script',':','void(0)'].join(''));
const root = path.resolve(app.vault.adapter.getBasePath());
const fileURI = (relative) => pathToFileURL(path.resolve(root, relative)).href;
handler.activate({}, fileURI('features/a draft ü.md'));
handler.activate({metaKey: true}, fileURI('features/meta.md'));
handler.activate({ctrlKey: true}, fileURI('features/ctrl.md'));
handler.activate(null, fileURI('features/child/../normal.md'));
handler.activate({}, `${pathToFileURL(root).href}/features/../inside.md`);
for (const uri of [fileURI('../outside.md'), fileURI('../vault-sibling/file.md'),
                   `${pathToFileURL(root).href}/../escape.md`,
                   `${pathToFileURL(root).href}/%2e%2e/escape.md`,
                   `${pathToFileURL(root).href}/%2e%2e%2fescape.md`,
                   `${pathToFileURL(root).href}/%ZZ.md`, pathToFileURL(root).href,
                   path.join(root, 'features/bare.md'), 'features/relative.md']) handler.activate({}, uri);
Object.assign(out, {opened, fileOpened, linkOpened, windowOpened, ribbons: plugin.ribbons.map((r) => r.title)});
  await plugin.ribbons[1].cb();
  out.profile = viewState && viewState.state['terminal:terminal'].profile;
  out.notices = [...notices];
  failNext = true;
  await plugin.ribbons[0].cb();  // a Terminal that refuses the view
  out.leaves = created.map((l) => ({pinned: l.pinned, detached: l.detached}));
  out.failureNotices = notices.slice(out.notices.length);
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runs the harness plugin")
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_the_manager_plugin_on_each_os(platform: str):
    """The ribbon works on every OS, Windows gets its PowerShell manager profile,
    and every terminal opens web links, same-vault Obsidian links and in-vault file URIs."""
    plugin = Path(__file__).resolve().parents[1] / "harness/obsidian/plugins/agent-terminal-focus/main.js"
    result = subprocess.run(
        ["node", "-e", _PLUGIN_HARNESS, str(plugin), platform],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    out = json.loads(result.stdout)
    assert out["restored"], "a terminal restored at startup gets the handler"
    assert not out["slowBeforeSweep"] and out["slowAfterSweep"], (
        "an emulator created late gets it on the sweep"
    )
    # Audit plan.md:71 (§B3) replaces immediate layout scans with a 250 ms debounce.
    assert not out["freshBeforeDebounce"] and out["fresh"], "layout-change patches after the debounce"
    assert out["allowNonHttpProtocols"] is True
    assert out["opened"] == [
        "https://auth.openai.com/oauth?x=1",
        "http://localhost:1455/cb",
        "obsidian://open?vault=My%20Vault&file=features%2F001-x%2Forders%2F001-mac-01.md",
    ]
    assert out["windowOpened"] == []
    assert out["linkOpened"] == []
    # Audit 10 plan §A2 retires dropping in-vault file URIs; outside paths stay dropped.
    assert out["fileOpened"] == [
        ["features/a draft ü.md", False],
        ["features/meta.md", True],
        ["features/ctrl.md", True],
        ["features/normal.md", False],
        ["inside.md", False],
    ]
    assert out["ribbons"] == ["New Codex manager", "New Claude manager"]
    assert out["notices"] == []
    # An opened manager tab is pinned. A refused view is detached and never pinned.
    assert out["leaves"] == [
        {"pinned": True, "detached": False},
        {"pinned": False, "detached": True},
    ]
    assert len(out["failureNotices"]) == 1 and "Could not open codex manager" in out["failureNotices"][0]
    profile = out["profile"]
    if platform == "win32":
        assert profile["executable"] == "powershell.exe"
        assert profile["platforms"] == {"win32": True}
        assert profile["pythonExecutable"] == "C:\\vault\\.agent\\terminal-python\\Scripts\\python.exe"
        assert "claude" in profile["args"][-1]
    else:
        assert profile["executable"] == ("/bin/zsh" if platform == "darwin" else "/bin/bash")
        assert profile["platforms"][platform] is True
