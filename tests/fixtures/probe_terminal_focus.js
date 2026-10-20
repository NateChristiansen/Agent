// Exercise the registered DOM listener, layout timers and snapshot readers without Obsidian.
const assert = require('assert/strict');
const Module = require('module');
const path = require('path');
const [pluginPath, platform = 'darwin'] = process.argv.slice(2);
Object.defineProperty(process, 'platform', {value: platform});
const callbacks = {}, ribbons = [], intervals = [], disposers = [], timers = new Map();
let timerId = 0, scans = 0;
global.setTimeout = (fn, ms) => { timers.set(++timerId, {fn, ms}); return timerId; };
global.clearTimeout = (id) => timers.delete(id);
global.window = new EventTarget();
window.setInterval = (fn, ms) => { intervals.push({fn, ms}); return intervals.length; };
global.document = new EventTarget();
class PluginStub {
  register(fn) { disposers.push(fn); }
  registerEvent() {}
  registerInterval() {}
  registerMarkdownCodeBlockProcessor() {}
  registerObsidianProtocolHandler() {}
  addRibbonIcon(icon, title, cb) { ribbons.push({icon, title, cb}); }
  registerDomEvent(target, name, fn, options) {
    target.addEventListener(name, fn, options);
    disposers.push(() => target.removeEventListener(name, fn, options));
  }
}
const files = new Map(), reads = new Map(), writes = new Map();
const file = (name) => path.join('/probe-vault', '.agent', name);
const put = (name, value, mtime = 1, ino = 1) => files.set(file(name), {body:JSON.stringify(value), mtime, ino});
const missing = () => Object.assign(new Error('missing'), {code:'ENOENT'});
let readGate = null;
const fsStub = {
  watch() { return {on(){}, close(){}}; },
  mkdirSync() {},
  appendFileSync(name, body) { writes.set(name, body); },
  writeFileSync(name, body) { writes.set(name, body); },
  renameSync(from, to) { writes.set(to, writes.get(from)); },
  promises: {
    async stat(name) {
      if (name === file('')) return {};
      const entry = files.get(name);
      if (!entry) throw missing();
      return {mtimeMs:entry.mtime, size:Buffer.byteLength(entry.body), ino:entry.ino};
    },
    async readFile(name) {
      reads.set(name, (reads.get(name) || 0) + 1);
      const body = files.get(name)?.body;
      if (readGate && name === file('live.json')) await readGate;
      if (body === undefined) throw missing();
      return body;
    },
    async readdir() { throw missing(); },
  },
};
const load = Module._load;
Module._load = function(request, ...args) {
  if (request === 'obsidian') return {Plugin:PluginStub, Modal:class {}, MarkdownRenderChild:class {}, addIcon(){}};
  if (request === 'fs') return fsStub;
  return load.call(this, request, ...args);
};
const Plugin = require(path.resolve(pluginPath));
const calls = [];
let focused = true;
const view = {getViewType:()=> 'vscode-editor', monacoEditor:{hasTextFocus:()=>focused, trigger:(...args)=>calls.push(args)}};
const leaf = {view};
const terminal = {view:{emulator:{terminal:{options:{}}}}};
const workspace = {
  activeLeaf:leaf, layoutReady:true,
  onLayoutReady() {},
  on(name, fn) { (callbacks[name] ||= []).push(fn); },
  getLeavesOfType() { scans++; return [terminal]; },
};
const plugin = new Plugin();
plugin.app = {workspace, vault:{adapter:{getBasePath:()=>'/probe-vault'}, on(){}}};
// Avoid the unrelated Windows process sweeper in this DOM probe.
plugin.startOrphanSweep = () => {};
const launched = [];
plugin.openManager = async (client) => { launched.push(client); };
plugin.onload();
assert.deepEqual(ribbons.map((r) => r.title), ['New Codex manager', 'New Claude manager']);
for (const r of ribbons) r.cb();
assert.deepEqual(launched, ['codex', 'claude'], 'each ribbon launches its own client only');
let defaults = 0;
window.addEventListener('keydown', () => defaults++);
const press = (fields = {}) => {
  const event = new Event('keydown', {cancelable:true, bubbles:true});
  Object.assign(event, {key:'f', metaKey:true, ctrlKey:false, altKey:false, shiftKey:false}, fields);
  window.dispatchEvent(event);
  return event.defaultPrevented;
};
assert.equal(press(), platform === 'darwin');
assert.equal(calls.length, platform === 'darwin' ? 1 : 0);
if (platform === 'darwin') {
  assert.deepEqual(calls[0], ['agent-terminal-focus', 'actions.find', null]);
  assert.equal(defaults, 0, 'the capture handler stops downstream shortcut listeners');
}
let untouched = 0;
const initialCalls = calls.length, initialDefaults = defaults;
for (const fields of [{metaKey:false,ctrlKey:true}, {shiftKey:true}, {altKey:true}, {ctrlKey:true}, {key:'g'}]) {
  assert.equal(press(fields), false); untouched++;
}
focused = false;
assert.equal(press(), false); untouched++;
focused = true;
for (const type of ['markdown', 'terminal:terminal']) {
  workspace.activeLeaf = {view:{getViewType:()=>type}};
  assert.equal(press(), false); untouched++;
}
workspace.activeLeaf = null;
assert.equal(press(), false); untouched++;
assert.equal(calls.length, initialCalls);
assert.equal(defaults, initialDefaults + untouched);
workspace.activeLeaf = leaf;
assert.equal(press({key:'F'}), platform === 'darwin');

const layout = () => callbacks['layout-change'].forEach((fn) => fn());
for (let i = 0; i < 20; i++) layout();
assert.equal(scans, 0);
assert.equal(timers.size, 1);
const timer = [...timers.values()][0];
assert.equal(timer.ms, 250);
timers.clear(); timer.fn();
assert.equal(scans, 1);
assert(terminal.view.emulator.terminal.options.linkHandler);

(async () => {
  const updates = [];
  plugin.livePanes.add({update(...args){updates.push(args);}});
  put('live.json', {tabs:[], managers:[{name:'first'}]});
  put('status.json', {features:[{id:1}]});
  await plugin.refreshLive();
  await plugin.refreshLive();
  assert.equal(reads.get(file('live.json')), 1);
  assert.equal(reads.get(file('status.json')), 1);
  assert.strictEqual(updates[0][0], updates[1][0]);
  assert.strictEqual(updates[0][3], updates[1][3]);
  put('live.json', {tabs:[], managers:[{name:'changed'}]}, 2);
  await plugin.refreshLive();
  assert.equal(updates.at(-1)[0].managers[0].name, 'changed');
  assert.equal(reads.get(file('live.json')), 2);
  assert.equal(reads.get(file('status.json')), 1);
  put('status.json', {features:[{id:10}]});
  await plugin.refreshLive();
  assert.equal(updates.at(-1)[3].features[0].id, 10, 'same-mtime size changes are reread');
  assert.equal(reads.get(file('status.json')), 2);
  put('status.json', {features:[{id:20}]}, 1, 2);
  await plugin.refreshLive();
  assert.equal(updates.at(-1)[3].features[0].id, 20, 'same-mtime same-size replacements are reread');
  assert.equal(reads.get(file('status.json')), 3);
  files.set(file('status.json'), {body:'{', mtime:2});
  await plugin.refreshLive();
  assert(updates.at(-1)[4].includes('snapshot unreadable'));
  assert.equal(updates.at(-1)[3].features[0].id, 20);
  put('status.json', {features:[{id:2}]}, 2);
  await plugin.refreshLive();
  assert.equal(updates.at(-1)[3].features[0].id, 2, 'failed parse retries even at the same mtime');
  files.delete(file('live.json'));
  await plugin.refreshLive();
  assert(updates.at(-1)[2].includes('snapshot unreadable'));
  assert.equal(updates.at(-1)[0].managers[0].name, 'changed');
  put('live.json', {tabs:[], managers:[{name:'in flight'}]}, 3);
  let release;
  readGate = new Promise((resolve) => { release = resolve; });
  const pending = plugin.refreshLive();
  await new Promise(setImmediate);
  put('live.json', {tabs:[], managers:[{name:'latest'}]}, 4);
  assert.strictEqual(plugin.refreshLive(), pending);
  release();
  await pending;
  readGate = null;
  assert.equal(updates.at(-1)[0].managers[0].name, 'latest', 'a file event during a read is retained');

  // The painter reuses bytes, but still evaluates snapshot authority and deadlines each tick.
  const painter = new Plugin.TabPainter({getLeavesOfType:()=>[]}, '/probe-vault', 'test', fsStub);
  const first = painter.poll(100000);
  assert.strictEqual(painter.poll(100001), first);
  assert.equal((await first).read_ok, true);
  const before = reads.get(file('live.json'));
  assert.equal((await painter.poll(102000)).read_ok, true);
  assert.equal(reads.get(file('live.json')), before);
  files.delete(file('live.json'));
  assert.equal((await painter.poll(104000)).read_ok, false);
  assert.equal(JSON.parse(writes.get(file('tabs/plugin.json'))).read_ok, false);
  put('live.json', {tabs:[]}, 4);
  assert.equal((await painter.poll(106000)).read_ok, true);
  const name = 'probe manager';
  const tab = {view:{state:{userTitle:`idle ${name}`}}};
  const entry = {key:'manager:probe',match:name,label:`waiting ${name}`,before_label:`idle ${name}`,
    stale_label:`waiting? ${name}`,show_after:new Date(110000).toISOString(),valid_until:new Date(112000).toISOString()};
  put('live.json', {tabs:[entry]}, 5);
  const deadlines = new Plugin.TabPainter({getLeavesOfType:()=>[tab]}, '/probe-vault', 'test', fsStub);
  await deadlines.poll(108000);
  const cached = reads.get(file('live.json'));
  assert.equal(tab.view.state.userTitle, `idle ${name}`);
  await deadlines.poll(110000);
  assert.equal(tab.view.state.userTitle, `waiting ${name}`);
  await deadlines.poll(114000);
  assert.equal(tab.view.state.userTitle, `waiting? ${name}`);
  assert.equal(reads.get(file('live.json')), cached, 'deadlines advance without rereading the snapshot');
  put('live.json', {tabs:[{...entry, label:`changed ${name}`}]}, 6);
  readGate = new Promise((resolve) => { release = resolve; });
  const stopping = deadlines.poll(116000);
  await new Promise(setImmediate);
  const heartbeat = writes.get(file('tabs/plugin.json'));
  deadlines.stop();
  release();
  assert.equal(await stopping, null);
  assert.equal(await deadlines.poll(118000), null);
  assert.equal(tab.view.state.userTitle, `waiting? ${name}`, 'unload fences an in-flight paint');
  assert.equal(writes.get(file('tabs/plugin.json')), heartbeat);
  layout();
  assert.equal(timers.size, 1);
  disposers.forEach((fn)=>fn());
  assert.equal(timers.size, 0, 'unload cancels the layout debounce');
  console.log(JSON.stringify({platform, untouched, debounceScans:scans, cachedDeadlines:true, asyncSnapshots:true}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
