// Synthetic snapshot and DOM. Counts structural writes separately from ticking age text.
const assert = require('assert');
const Module = require('module');
const load = Module._load;
const callbacks = {}, disposers = [], intervals = [];
class PluginStub {
  registerView(type, make) { callbacks.view = make; }
  registerMarkdownCodeBlockProcessor(type, make) { callbacks.block = make; }
  addCommand(command) { callbacks.command = command; }
  register(fn) { disposers.push(fn); }
  registerInterval(id) {}
  registerEvent(event) {}
  registerDomEvent() {}
  registerObsidianProtocolHandler() {}
  addRibbonIcon() {}
}
class El {
  constructor(tag, text = '') { this.tagName = tag; this.textContent = text; this.children = []; this.style = {}; this.attrs = {}; this.events = {}; }
  createEl(tag, opts = {}) { if (tag === 'tr') creations++; const el = new El(tag, opts.text); this.appendChild(el); return el; }
  createSpan(opts) { return this.createEl('span', opts); }
  appendChild(el) { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1); this.children.push(el); el.parent = this; }
  insertBefore(el, before) { moves++; if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1); this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, el); el.parent = this; }
  replaceChildren() { patches.push(this); this.children = []; }
  setText(text) { this.textContent = text; }
  setAttr(key, value) { this.attrs[key] = value; }
  addEventListener(key, fn) { this.events[key] = fn; }
  remove() { if (this.tagName === 'tr') removals++; this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
Module._load = function(request, ...rest) {
  if (request === 'obsidian') return { Plugin: PluginStub, Modal: class {}, MarkdownRenderChild: class {}, Notice: class {}, addIcon() {} };
  if (request === 'fs') return {
    watch(_path, fn) { callbacks.file = fn; return {on(){},close(){callbacks.closed=true;}}; },
    promises: {
      async stat() { return {mtimeMs: revision}; },
      async readFile(file) {
        if (String(file).endsWith('status.json') && !statusReadable) throw new Error('ordinary read failure');
        return JSON.stringify(String(file).endsWith('status.json') ? statusValid ? status : null : snap);
      },
      async readdir() { return []; },
    },
  };
  return load.call(this, request, ...rest);
};
const Plugin = require(require('path').resolve(process.argv[2]));
let patches = [], moves = 0, creations = 0, removals = 0, now = Date.parse('2026-10-14T05:00:00Z'), statusReadable = true, statusValid = true, revision = 1;
const snap = { rendered_at: new Date(now).toISOString(), valid_until: new Date(now + 100000).toISOString(),
  managers: [ {session_id:'b', feature:'002 two', name:'second', state:'working', since:new Date(now).toISOString(), leases:['vault:x'], clear_leases:true}, {session_id:'a', feature:'001 one', name:'first', state:'approval', attention:'approval', show_after:new Date(now + 20000).toISOString(), since:new Date(now).toISOString()} ],
  needs_you: [{kind:'manager',session_id:'a',name:'first',reason:'approval',show_after:new Date(now + 20000).toISOString()}, {kind:'orphaned',feature:3,count:1,ids:['003-01'],resume:{command:'claude --resume ordinary'}}],
  workers: Array.from({length:47}, (_,i) => ({id:`004-${String(i).padStart(2,'0')}`, feature:4, state:'running', started:new Date(now - 1000).toISOString(), goal:'ordinary work', handoff:`features/004/handoffs/${i}.md`, open:'obsidian://agent-dispatch-open?id=ordinary'})),
  health: {schema:2, rows:[
    {key:'hook_errors:mac', title:'Hooks · mac', brief:'Hooks · mac', state:'ok', headline:'no errors today', ages:[], order:[2,2,'mac'], updated:new Date(now).toISOString(), valid_until:new Date(now + 100000).toISOString(), expired:null},
    {key:'notes_sync:mac', title:'Notes sync · mac', brief:'Notes sync · mac', state:'action', headline:'conflict since 04:58', ages:[{prefix:' · last sync ', at:new Date(now - 60000).toISOString(), suffix:' ago'}], meaning:'ordinary meaning', action:'ordinary action', order:[0,0,'mac'], updated:new Date(now).toISOString(), valid_until:new Date(now + 100000).toISOString(), expired:{state:'unknown', headline:'last report: conflict since 04:58', action:'refresh'}},
    {key:'machine:mac', title:'mac', brief:'mac', state:'unknown', headline:'no report', ages:[{prefix:' for ', at:new Date(now).toISOString(), suffix:''}], order:[1,5,'mac'], show_after:new Date(now + 100000).toISOString(), valid_until:null, expired:null},
  ]}
};
const status = {features:[
  {id:1,dir:'features/001-one',slug:'one',title:'One',status:'active',open_tasks:2,open_task_items:[{index:2,text:'ordinary task'},{index:10,text:'later task'}]},
  {id:2,dir:'features/002-two',slug:'two',title:'Two',status:'blocked',open_tasks:0,open_task_items:[]},
]};
const opened = [];
const app = {workspace:{openLinkText: (...args) => opened.push(args)}};
const root = new El('root');
const pane = new Plugin.LivePane(root, app);
pane.update(snap, now, '', status);
assert.deepEqual(root.children.filter((el)=>el.tagName==='h2').map((el)=>el.textContent), ['Needs you','Managers','Workers','Open features (2)','Open tasks (2)','Health: 1 needs action']);
assert.deepEqual([...pane.groups.get('health').rows.keys()], ['notes_sync:mac']);
assert.equal(pane.healthSummary.textContent, '1 ok: Hooks · mac');
const healthRow = pane.groups.get('health').rows.get('notes_sync:mac').el;
assert.deepEqual(pane.groups.get('tasks').body.children.map((el)=>el.attrs['data-key']), ['sec:1','1:2','1:10']);
assert.equal(pane.healthDetails.tagName, 'details');
assert(!pane.healthDetails.attrs.open);
const first = pane.groups.get('workers').rows.get('004-00').el;
const container = pane.groups.get('workers').body;
patches = []; moves = 0; creations = 0; removals = 0;
for (let i = 0; i < 3; i++) { now += 2000; pane.update(snap, now); }
assert.equal(patches.length, 0);
assert.equal(moves, 0);
assert.equal(creations, 0);
assert.equal(removals, 0);
assert.strictEqual(first, pane.groups.get('workers').rows.get('004-00').el);
assert.strictEqual(container, pane.groups.get('workers').body);
snap.workers[0].goal = 'changed cell';
pane.update(snap, now);
assert.equal(patches.length, 1);
assert.strictEqual(patches[0].parent, first);
patches = []; moves = 0;
snap.managers.reverse();
snap.managers[0].activity = {at:'2026-10-14T05:30:00Z'};
snap.managers[0].since = snap.managers[0].activity.at;
pane.update(snap, now);
assert.equal(moves, 0);
assert.equal(patches.length, 1);
assert.strictEqual(patches[0].parent, pane.groups.get('managers').rows.get('a').el);
const managerOrder = pane.groups.get('managers').body.children.map((el)=>el.attrs['data-key']);
assert.deepEqual(managerOrder, ['a','b']);
pane.update(snap, now + 20000);
assert(pane.groups.get('needs_you').rows.has('manager:a'));
assert.equal(pane.groups.get('managers').rows.get('a').cells[1].el.children[0].textContent, 'approval');
pane.update(snap, now + 100001);
assert.equal(pane.groups.get('workers').rows.get('004-00').cells[1].el.children[0].textContent, 'unknown');
// Past valid_until the problem stays as unknown history, the healthy row folds into the
// machine's "no report" row, and the history row keeps its node.
assert.strictEqual(pane.groups.get('health').rows.get('notes_sync:mac').el, healthRow);
assert.equal(healthRow.children[1].children[0].textContent, 'unknown · last report: conflict since 04:58');
assert.deepEqual(pane.groups.get('health').body.children.map((el)=>el.attrs['data-key']), ['notes_sync:mac','machine:mac']);
assert.equal(pane.healthDetails.style.display, 'none');
assert.equal(root.children.find((el)=>el.textContent.startsWith('Health:')).textContent, 'Health: 2 unknown');
const handoff = pane.groups.get('workers').rows.get('004-00').cells[0].el.children[0];
handoff.events.click({preventDefault(){},metaKey:false,ctrlKey:true});
assert.equal(opened[0][0], 'features/004/handoffs/0.md');
assert.equal(opened[0][2], true);
(async () => {
global.window = {setInterval(fn, ms){intervals.push({fn,ms});return intervals.length;},clearInterval(){}};
global.document = {};
const plugin = new Plugin();
plugin.app = {vault:{adapter:{getBasePath:()=>'/ordinary-vault'},on(event,fn){callbacks.vault=fn;}},workspace:{onLayoutReady(){},on(){},getLeavesOfType(){return [];}}};
plugin.onload();
let block;
callbacks.block('',new El('embed'),{addChild(child){block=child;child.onload();}});
// Audit plan.md:68 (§B2) retires standalone tabs; the Dashboard block owns the renderer.
assert.equal(callbacks.view, undefined);
assert.equal(callbacks.command, undefined);
await plugin.refreshLive();
assert.equal(plugin.livePanes.size,1);
snap.workers[0].goal = 'file event changed goal';
revision++;
callbacks.file('rename','live.json');
await plugin.liveRefresh;
assert.equal([...plugin.livePanes][0].groups.get('workers').rows.get('004-00').cells[3].el.children[0].textContent,'file event changed goal');
snap.workers[0].goal = 'vault event changed goal';
revision++;
callbacks.vault({path:'.agent/live.json'});
await plugin.liveRefresh;
assert.equal([...plugin.livePanes][0].groups.get('workers').rows.get('004-00').cells[3].el.children[0].textContent,'vault event changed goal');
assert(intervals.some((i)=>i.ms===2000));
status.features[0].open_task_items[0].text = 'status file event';
revision++;
callbacks.file('rename', 'status.json');
await plugin.liveRefresh;
assert.equal([...plugin.livePanes][0].groups.get('tasks').rows.get('1:2').cells[0].el.children[0].textContent, '#2 status file event');
status.features[0].open_task_items[0].text = 'status vault event';
revision++;
callbacks.vault({path:'.agent/status.json'});
await plugin.liveRefresh;
assert.equal([...plugin.livePanes][0].groups.get('tasks').rows.get('1:2').cells[0].el.children[0].textContent, '#2 status vault event');
statusReadable = false;
snap.workers[0].goal = 'live update despite status read failure';
revision++;
callbacks.file('rename','live.json');
await plugin.liveRefresh;
assert([...plugin.livePanes][0].warning.textContent.includes('feature/task snapshot unreadable'));
assert.equal([...plugin.livePanes][0].groups.get('workers').rows.get('004-00').cells[3].el.children[0].textContent, 'live update despite status read failure');
assert([...plugin.livePanes][0].groups.get('tasks').rows.has('1:2'));
statusReadable = true;
statusValid = false;
snap.workers[0].goal = 'live update despite invalid status';
revision++;
callbacks.file('rename','status.json');
await plugin.liveRefresh;
assert([...plugin.livePanes][0].warning.textContent.includes('feature/task snapshot unreadable'));
assert.equal([...plugin.livePanes][0].groups.get('workers').rows.get('004-00').cells[3].el.children[0].textContent, 'live update despite invalid status');
assert([...plugin.livePanes][0].groups.get('tasks').rows.has('1:2'));
statusValid = true;
block.onunload();
assert.equal(plugin.livePanes.size,0);
disposers.forEach((fn)=>fn());
assert(callbacks.closed);
const featureRow = pane.groups.get('features').rows.get('1').el;
const taskRow = pane.groups.get('tasks').rows.get('1:2').el;
pane.update(snap, now, '', status);
patches = []; moves = 0;
status.features[0].open_task_items[0].text = 'changed canonical text';
pane.update(snap, now, '', status);
assert.equal(patches.length, 1);
assert.strictEqual(patches[0].parent, taskRow);
assert.strictEqual(pane.groups.get('features').rows.get('1').el, featureRow);
patches = [];
for (let i=0; i<3; i++) pane.update(snap, now, '', status);
assert.equal(patches.length, 0);
assert.equal(moves, 0);
status.features[0].open_tasks = 1;
status.features[0].open_task_items.pop();
pane.update(snap, now, '', status);
assert.strictEqual(pane.groups.get('tasks').rows.get('1:2').el, taskRow);
assert(!pane.groups.get('tasks').rows.has('1:10'));
const result = {polls:3,containerReplacements:0,rowReplacements:0,changedCellPatches:1,activityReorders:0,workers:47,deadlineChecks:2};

// Mixed ids/statuses exercise the same keyed renderer, including section counts and moves.
const grouped = new Plugin.LivePane(new El('root'), app);
const feature = (id, state, extra = {}) => ({id, title:`Feature ${id}`, dir:`features/${id}`, status:state,
  open_tasks:2, open_task_items:[{index:10,text:'later'},{index:2,text:'earlier'}], ...extra});
const mixed = {features:[
  feature(1,'proposed'), feature(2,'blocked',{parked:true}), feature(3,'active',{parked:true}),
  feature(10,'active'), feature(20,'blocked'), feature(30,'active'),
  feature(40,'active',{open_tasks:0,done_tasks:1,open_task_items:[]}),
  feature(50,'active',{parked:true,open_tasks:0,done_tasks:1,open_task_items:[]}),
  feature(60,'proposed',{parked:true}), feature(70,'closed'),
  feature(80,'blocked',{blocked_work:['worker running'],blocker:{reason:'First reason',waits_on:'owner'}}),
]};
const keys = (key) => grouped.groups.get(key).body.children.map((el)=>el.attrs['data-key']);
const cellText = (key, id, cell = 0) => grouped.groups.get(key).rows.get(id).cells[cell].el.children[0].textContent;
const headings = Object.fromEntries(['features','tasks'].map((key)=>[key,grouped.groups.get(key).heading]));
grouped.update({}, now, '', mixed);
assert.deepEqual(keys('features'), ['sec:active','40','10','30','3','50','sec:blocked','20','80','2','sec:proposed','1','60']);
assert.deepEqual(['sec:active','sec:blocked','sec:proposed'].map((id)=>cellText('features',id)), ['Active (5)','Blocked (3)','Proposed (2)']);
assert.deepEqual(keys('tasks'), [10,30,3,20,80,2,1,60].flatMap((id)=>[`sec:${id}`,`${id}:2`,`${id}:10`]));
assert.equal(headings.features.textContent, 'Open features (10)');
assert.equal(headings.tasks.textContent, 'Open tasks (16)');
assert.equal(cellText('features','40',2), 'ready to close');
assert.equal(cellText('features','50',2), 'parked · ready to close');
assert.equal(cellText('features','80',2), 'blocked: First reason · next: owner · active work: worker running');
const blockedFeature = mixed.features.find((f)=>f.id===80);
const blockedRow = grouped.groups.get('features').rows.get('80').el;
blockedFeature.blocker = {reason:'Current reason',waits_on:'external'};
grouped.update({}, now, '', mixed);
assert.equal(cellText('features','80',2), 'blocked: Current reason · next: external · active work: worker running');
assert.strictEqual(grouped.groups.get('features').rows.get('80').el, blockedRow);
delete blockedFeature.blocker;
blockedFeature.blocked_since = {reason:'Legacy reason'};
grouped.update({}, now, '', mixed);
assert.equal(cellText('features','80',2), 'blocked: Legacy reason · next: — · active work: worker running');
const sectionCell = grouped.groups.get('features').rows.get('sec:active').cells[0].el;
assert.equal(sectionCell.colSpan, 3);
assert.equal(sectionCell.style.fontWeight, 'bold');
assert.equal(sectionCell.style.background, 'var(--background-secondary)');
assert.equal(grouped.groups.get('features').rows.get('sec:active').cells.length, 1);
assert.equal(cellText('tasks','sec:10'), '010 Feature 10 (2 open)');
const taskSection = grouped.groups.get('tasks').rows.get('sec:10').cells[0].el;
assert.equal(taskSection.colSpan, 1);
assert.equal(taskSection.children[0].attrs.href, 'features/10/plan.md');
taskSection.children[0].events.click({preventDefault(){},metaKey:false,ctrlKey:false});
assert.deepEqual(opened.at(-1), ['features/10/plan.md','',false]);

patches = []; moves = 0; creations = 0; removals = 0;
for (let i=0; i<3; i++) grouped.update({}, now, '', JSON.parse(JSON.stringify(mixed)));
assert.equal(patches.length + moves + creations + removals, 0);
const unchangedPolls = {rowCreations:creations,rowRemovals:removals};
const retained = new Map([...grouped.groups.get('tasks').rows].map(([id,row])=>[id,row.el]));
const changed = mixed.features.find((f)=>f.id===10);
changed.open_tasks = 1;
changed.open_task_items.pop();
grouped.update({}, now, '', mixed);
const taskPatches = patches.filter((el)=>el.parent.parent===grouped.groups.get('tasks').body);
assert.deepEqual(taskPatches.map((el)=>el.parent.attrs['data-key']), ['sec:10']);
assert.equal(removals, 1);
assert.equal(creations, 0);
assert(!grouped.groups.get('tasks').rows.has('10:2'));
assert.strictEqual(grouped.groups.get('tasks').rows.get('10:10').el, retained.get('10:10'));
assert.equal(cellText('tasks','sec:10'), '010 Feature 10 (1 open)');

const featureEl = grouped.groups.get('features').rows.get('10').el;
patches = []; moves = 0; creations = 0; removals = 0;
changed.status = 'proposed';
grouped.update({}, now, '', mixed);
assert.deepEqual(keys('features'), ['sec:active','40','30','3','50','sec:blocked','20','80','2','sec:proposed','1','10','60']);
assert.deepEqual(keys('tasks'), [30,3,20,80,2,1,10,60].flatMap((id)=>id===10 ? ['sec:10','10:10'] : [`sec:${id}`,`${id}:2`,`${id}:10`]));
assert(moves > 0);
assert.equal(creations + removals, 0);
assert.strictEqual(grouped.groups.get('features').rows.get('10').el, featureEl);
for (const [id,row] of grouped.groups.get('tasks').rows) assert.strictEqual(row.el,retained.get(id));
const statusChangeRebuilds = creations + removals;

// Explicit orders are opt-in. The original feature/key sort still orders every other group.
const other = {needs_you:[{kind:'orphaned',feature:20},{kind:'orphaned',feature:2}],
  managers:[{session_id:'z',feature:20},{session_id:'b',feature:2},{session_id:'a',feature:2}],
  workers:[{id:'w20',feature:20},{id:'w10',feature:2},{id:'w2',feature:2}],
  health:{rows:[{key:'notes_sync:z',state:'ok'},{key:'notes_sync:a',state:'ok'},{key:'hooks:a',state:'odd'}]}};
grouped.update(other,now,'',mixed);
assert.deepEqual(keys('needs_you'), ['orphaned:2','orphaned:20']);
assert.deepEqual(keys('managers'), ['a','b','z']);
assert.deepEqual(keys('workers'), ['w2','w10','w20']);
assert.deepEqual(keys('health'), ['hooks:a']);
assert.deepEqual(keys('health_ok'), ['notes_sync:a','notes_sync:z']);
grouped.update({managers:[]},now,'',mixed);
assert.deepEqual(keys('health'), ['health:']);
assert.deepEqual(keys('health_ok'), []);
grouped.update({},now,'',{features:[feature(1,'closed')]});
assert.deepEqual(keys('features'), []);
assert.deepEqual(keys('tasks'), []);
assert.equal(grouped.groups.get('features').empty.textContent, 'No open features.');
assert.equal(grouped.groups.get('tasks').empty.textContent, 'No open tasks.');
assert.equal(headings.features.textContent, 'Open features (0)');
assert.equal(headings.tasks.textContent, 'Open tasks (0)');
grouped.update({},now,'',{features:[feature(1,'active',{open_tasks:0,open_task_items:[]})]});
assert.deepEqual(keys('features'), ['sec:active','1']);
assert.deepEqual(keys('tasks'), []);
assert.equal(grouped.groups.get('tasks').empty.textContent, 'No open tasks.');
result.grouping = {features:10,tasks:16,...unchangedPolls,statusChangeRebuilds};
if (process.argv[3]) {
  const cases = JSON.parse(process.argv[3]);
  for (const {snapshot, expected, expectedNotes} of cases) {
    pane.update(snap, now, '', snapshot);
    if (expectedNotes !== undefined) assert.equal(pane.groups.get('features').rows.get('1').cells[2].el.children[0].textContent, expectedNotes);
    assert.equal(pane.groups.get('features').rows.get('1').cells[1].el.children[0].textContent, String(expected.length));
    const rows = pane.groups.get('tasks').body.children.filter((el)=>!el.attrs['data-key'].startsWith('sec:'));
    assert.deepEqual(rows.map((el) => pane.groups.get('tasks').rows.get(el.attrs['data-key']).cells[0].el.children[0].textContent), expected);
    assert.equal(pane.groups.get('tasks').heading.textContent, `Open tasks (${expected.length})`);
  }
  result.parityCases = cases.length;
}
console.log(JSON.stringify(result));
})().catch((error) => { console.error(error); process.exitCode = 1; });
