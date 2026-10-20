// The stale Dataview count is deliberately different from the Python snapshot.
const assert = require('assert'), fs = require('fs');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const snapshot = {features:[{id:9,dir:'features/009-ordinary',slug:'ordinary',title:'Ordinary',status:'active',open_tasks:2,
  open_task_items:[{index:2,text:'first ordinary task'},{index:5,text:'second ordinary task'}]}]};
const texts=[];
class El {
  createDiv(opts={}) { return this.createEl('div',opts); }
  createEl(_tag,opts={}) { if(opts.text) texts.push(opts.text); return new El(); }
  setAttr(){} addEventListener(){}
}
const dv={container:new El(),luxon:{DateTime:{now(){return {startOf(){return {};}};}}},
  pages(){throw new Error('Dataview task cache must not supply counts');},
  paragraph(t){texts.push(t);},list(rows){texts.push(...rows);}};
const app={vault:{adapter:{async read(){return JSON.stringify(snapshot);}}}};
(async()=>{
  await new AsyncFunction('dv','app',fs.readFileSync(process.argv[2],'utf8'))(dv,app);
  assert(texts.includes('2'));
  assert(!texts.includes('0'));
  assert(!texts.some((text)=>text.includes('reload')));
  texts.length=0;
  await new AsyncFunction('dv','app',fs.readFileSync(process.argv[3],'utf8'))(dv,app);
  assert(texts.includes('#2 first ordinary task'));
  assert(texts.includes('#5 second ordinary task'));
  console.log(JSON.stringify({canonicalCount:2,canonicalTaskNumbers:[2,5],dataviewReads:0}));
})().catch((error)=>{console.error(error);process.exitCode=1;});
