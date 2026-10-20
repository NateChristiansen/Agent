const { Plugin } = require('obsidian');

// Obsidian skips every path with a dot segment. Show Hidden Files 1.0.3 showed all of them, so
// the index held tens of thousands of files (node_modules, .venv, .git) and a start took most
// of a minute instead of a second. This plugin shows only the agent folders and never lists what is
// in SKIP. Lists in data.json ({"show": [...], "skip": [...]}) replace these defaults.
const SHOW = ['.agent', '.agents', '.claude', '.codex', '.github'];
// A name with no slash skips that folder at any depth; a path skips that one subtree.
const SKIP = ['node_modules', '.agent/mcp-servers'];
const NEVER = ['.trash'];

// Lower case throughout: Windows and the default macOS volume are case-insensitive, and a
// skipped folder spelled another way is still the same folder.
function segments(vaultPath) {
  return String(vaultPath).replace(/\\/g, '/').toLowerCase().split('/').filter(Boolean);
}

function shown(vaultPath, lists = {}) {
  const parts = segments(vaultPath);
  if (!parts.length) return false;
  const never = [...NEVER, ...(lists.never || [])].map((name) => name.toLowerCase());
  if (never.includes(parts[0])) return false;
  const show = (lists.show || SHOW).map((name) => segments(name)[0]);
  if (!show.includes(parts[0])) return false;
  const joined = parts.join('/');
  for (const entry of lists.skip || SKIP) {
    const skip = segments(entry);
    if (skip.length === 1 && parts.includes(skip[0])) return false;
    if (skip.length > 1) {
      const prefix = skip.join('/');
      if (joined === prefix || joined.startsWith(`${prefix}/`)) return false;
    }
  }
  return true;
}

function stringList(value) {
  return Array.isArray(value) && value.every((item) => typeof item === 'string') ? value : null;
}

module.exports = class AgentHiddenFiles extends Plugin {
  async onload() {
    this.original = null;
    this.shownPaths = new Map();
    this.unloaded = false;
    const data = (await this.loadData()) || {};
    this.lists = {
      show: stringList(data.show) || SHOW,
      skip: stringList(data.skip) || SKIP,
      never: [this.app.vault.configDir],
    };
    this.app.workspace.onLayoutReady(() => this.enable());
  }

  // Obsidian's scan sends each hidden path to reconcileDeletion and returns, without a stat or a
  // listing. A folder is listed only through reconcileFileInternal. So adding a shown path here
  // is what makes it appear, and passing a skipped folder to the original keeps the scan out of
  // it: every level under a dot folder comes back through this wrapper.
  async enable() {
    const adapter = this.app.vault.adapter;
    if (this.unloaded || this.original) return;
    if (!['reconcileDeletion', 'reconcileFileInternal', 'listRecursive'].every((name) => typeof adapter[name] === 'function')) {
      console.warn('agent-hidden-files: this Obsidian has no file reconcile hooks; hidden folders stay hidden');
      return;
    }
    const original = adapter.reconcileDeletion;
    this.original = original;
    const plugin = this;
    adapter.reconcileDeletion = async function (realPath, vaultPath, ...rest) {
      // After unload a later plugin's wrapper may still call this one; it must then do nothing.
      if (!plugin.unloaded && shown(vaultPath, plugin.lists)) {
        try {
          await adapter.reconcileFileInternal(realPath, vaultPath);
          plugin.shownPaths.set(vaultPath, realPath);
          return undefined;
        } catch (error) {
          if (!error || error.code !== 'ENOENT') throw error;
          plugin.shownPaths.delete(vaultPath);
        }
      }
      return original.call(adapter, realPath, vaultPath, ...rest);
    };
    this.wrapper = adapter.reconcileDeletion;
    try {
      await adapter.listRecursive('');
    } catch (error) {
      console.warn('agent-hidden-files: rescan failed', error);
    }
  }

  async onunload() {
    this.unloaded = true;
    const adapter = this.app.vault.adapter;
    const original = this.original;
    if (!original) return;
    // Another plugin may have wrapped the adapter after this one; leave its wrapper in place.
    if (adapter.reconcileDeletion === this.wrapper) adapter.reconcileDeletion = original;
    this.original = null;
    for (const [vaultPath, realPath] of this.shownPaths) {
      try {
        await original.call(adapter, realPath, vaultPath);
      } catch (error) {
        console.warn('agent-hidden-files: could not hide', vaultPath, error);
      }
    }
    this.shownPaths.clear();
  }
};

module.exports.shown = shown;
module.exports.SHOW = SHOW;
module.exports.SKIP = SKIP;
