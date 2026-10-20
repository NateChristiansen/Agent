const fs = require('fs');
const assert = require('assert');
class El {
  constructor(tag, text = '') { this.tag = tag; this.textContent = text; this.children = []; }
  createEl(tag, opts = {}) { const el = new El(tag, opts.text); this.children.push(el); el.parent = this; return el; }
  createSpan(opts) { return this.createEl('span', opts); }
  appendText(text) { this.textContent += text; }
  setText(text) { this.textContent = text; }
  insertBefore(el, before) { el.remove(); this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, el); el.parent = this; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  empty() { throw Error('worker container cleared'); }
}
let tick, now = 0;
let records = [{type:'assistant',text:'ordinary work',tools:['Read']}];
const root = new El('root');
const DateTime = {fromISO:()=>({toRelative:()=>`${now}s`})};
const dv = {container:root,luxon:{DateTime},component:{register(){}}};
const app = {vault:{adapter:{read:async (path)=>path.endsWith('live.json') ? JSON.stringify({workers:[{id:'001-01',state:'running',started:'2026-10-14T00:00:00Z'}]}) : records.map((r)=>JSON.stringify(r)).join('\n')}}};
const window = {setInterval(fn){tick=fn;return 1;},clearInterval(){}};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const run = new AsyncFunction('input','dv','window','app',fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
  await run({id:'001-01'},dv,window,app);
  const ul = root.children[1], first = ul.children[0], head = root.children[0];
  for(let i=0;i<3;i++){now+=3;await tick();}
  assert.strictEqual(root.children[0],head);
  assert.strictEqual(root.children[1],ul);
  assert.strictEqual(ul.children[0],first);
  records.push({type:'assistant',text:'next ordinary work',tools:['Edit']});
  await tick();
  assert.equal(ul.children.length,2);
  assert.strictEqual(ul.children[0],first);
  console.log(JSON.stringify({polls:3,containerClears:0,retainedRows:1,addedRows:1}));
})();
