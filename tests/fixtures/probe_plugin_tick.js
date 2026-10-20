// Time snapshot/painter ticks; with --baseline the file given is an older build to compare. No real vault or .agent directory is used.
// The no-op pane isolates I/O and parsing; this is not an Obsidian first-click profile.
const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');
const {performance} = require('perf_hooks');
const pluginPath = path.resolve(process.argv[2]);
const baseline = process.argv.includes('--baseline');
const source = fs.readFileSync(pluginPath, 'utf8');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-plugin-tick-'));
const virtualVault = '/probe-vault';
const translate = (file) => path.join(temp, path.relative(path.join(virtualVault, '.agent'), file));
let snapshotReads = 0, syncCalls = 0;
const isSnapshot = (name) => ['live.json', 'status.json'].includes(path.basename(name));
const io = {promises:{}};
for (const method of ['stat', 'readFile', 'readdir', 'mkdir', 'appendFile', 'writeFile', 'rename']) {
  io.promises[method] = (...args) => {
    if (method === 'readFile' && isSnapshot(args[0])) snapshotReads++;
    args[0] = translate(args[0]);
    if (method === 'rename') args[1] = translate(args[1]);
    return fs.promises[method](...args);
  };
}
for (const method of ['existsSync', 'readFileSync', 'readdirSync', 'mkdirSync', 'appendFileSync', 'writeFileSync', 'renameSync']) {
  io[method] = (...args) => {
    syncCalls++;
    if (method === 'readFileSync' && isSnapshot(args[0])) snapshotReads++;
    args[0] = translate(args[0]);
    if (method === 'renameSync') args[1] = translate(args[1]);
    return fs[method](...args);
  };
}
const load = Module._load;
Module._load = function(request, ...args) {
  if (request === 'obsidian') return {Plugin:class {}, Modal:class {}};
  if (request === 'fs') return io;
  return load.call(this, request, ...args);
};
const mod = new Module(pluginPath, module);
mod.filename = pluginPath;
mod.paths = module.paths;
mod._compile(source, pluginPath);
const Plugin = mod.exports;
const sized = (value, bytes) => {
  value.padding = '';
  value.padding = 'x'.repeat(bytes - Buffer.byteLength(JSON.stringify(value)));
  return JSON.stringify(value);
};
const live = sized({tabs:[], managers:[], workers:[]}, 6126);
const status = sized({features:Array.from({length:100}, (_,id)=>({id, status:'active', open_task_items:[]}))}, 98171);
fs.writeFileSync(path.join(temp, 'live.json'), live);
fs.writeFileSync(path.join(temp, 'status.json'), status);
const plugin = new Plugin();
plugin.app = {vault:{adapter:{getBasePath:()=>virtualVault}}};
plugin.livePanes = new Set([{update(snap, now, error, status, statusError){
  assert.equal(error, '');
  assert.equal(statusError, '');
  assert.equal(snap.tabs.length, 0);
  assert.equal(status.features.length, 100);
}}]);
// Use onload's real reader initialization without installing timers, watchers or UI hooks.
if (!baseline) {
  const setup = new Plugin();
  setup.app = {vault:{adapter:{getBasePath:()=>virtualVault}, on(){}}, workspace:{layoutReady:true,onLayoutReady(){},on(){}}};
  for (const name of ['registerMarkdownCodeBlockProcessor','registerDomEvent','registerEvent','registerInterval','register','registerObsidianProtocolHandler','addRibbonIcon']) setup[name] = () => {};
  io.watch = () => ({on(){},close(){}});
  global.document = {};
  global.window = {setInterval(){}};
  // Disable ribbon icon and platform-specific setup; this does not alter tick behavior.
  Object.defineProperty(process, 'platform', {value:'probe'});
  setup.onload();
  plugin.liveReader = setup.liveReader;
  plugin.statusReader = setup.statusReader;
}
plugin.tabPainter = new Plugin.TabPainter({getLeavesOfType:()=>[]}, virtualVault, 'probe', io);
let entryTime = 0;
const tick = async (now) => {
  let start = performance.now();
  const refresh = plugin.refreshLive();
  entryTime += performance.now() - start;
  await refresh;
  start = performance.now();
  const paint = plugin.tabPainter.poll(now);
  entryTime += performance.now() - start;
  assert.equal((await paint).read_ok, true);
  await new Promise(setImmediate);
};
(async () => {
  try {
    for (let i=0; i<20; i++) await tick(100000 + i*2000);
    snapshotReads = 0; syncCalls = 0; entryTime = 0;
    const initial = performance.eventLoopUtilization();
    const start = performance.now();
    const ticks = 300;
    for (let i=0; i<ticks; i++) await tick(200000 + i*2000);
    const elapsed = performance.now() - start;
    const active = performance.eventLoopUtilization(initial).active;
    assert.equal(snapshotReads, baseline ? ticks*3 : 0);
    console.log(JSON.stringify({revision:baseline ? 'baseline' : 'worktree', ticks, liveBytes:live.length, statusBytes:status.length,
      meanEntryMs:entryTime/ticks, meanElapsedMs:elapsed/ticks, meanEventLoopActiveMs:active/ticks, snapshotReads, syncFsCalls:syncCalls}));
  } finally { fs.rmSync(temp, {recursive:true,force:true}); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
