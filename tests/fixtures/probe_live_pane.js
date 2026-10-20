// Exercise the plugin's real LivePane with a minimal DOM and a controlled clock.
const Module = require('module');
const load = Module._load;
Module._load = function(request, ...rest) {
  if (request === 'obsidian') return {Plugin: class {}, Modal: class {}, ItemView: class {}, MarkdownRenderChild: class {}, Notice: class {}};
  return load.call(this, request, ...rest);
};
class El {
  constructor(tag, text = '') { this.tag = tag; this.textContent = text; this.children = []; this.style = {}; this.attrs = {}; }
  createEl(tag, opts = {}) { const el = new El(tag, opts.text); this.appendChild(el); return el; }
  appendChild(el) { if (el.parent) el.remove(); this.children.push(el); el.parent = this; }
  insertBefore(el, before) { if (el.parent) el.remove(); this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, el); el.parent = this; }
  replaceChildren() { this.children = []; }
  setText(text) { this.textContent = text; }
  setAttr(key, value) { this.attrs[key] = value; }
  addEventListener() {}
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
const Plugin = require(require('path').resolve(process.argv[2]));
const {snapshot, section, now, seconds, prior} = JSON.parse(process.argv[3]);
const root = new El('root');
const pane = new Plugin.LivePane(root, {});
if (prior) pane.update(prior, now);
pane.update(snapshot, now);
pane.tick(now + seconds * 1000);
const nodes = [];
function walk(el) {
  nodes.push(el.attrs.href ? [el.tag, el.textContent, el.attrs.href] : [el.tag, el.textContent]);
  el.children.forEach(walk);
}
walk(pane.warning);
if (section) {
  const group = pane.groups.get(section);
  walk(group.body);
  walk(group.empty);
} else walk(root);
console.log(JSON.stringify(nodes));
