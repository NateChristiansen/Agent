// Recent log entries across open features, newest first, as the callouts they are in log.md.
//
// Log entries are callouts written by `bin/agent log` (`src/agent_cli/log.py`, HEADER_RE):
//   > [!kind] YYYY-MM-DD HH:MM
//   > text
// Dataview's query language cannot see a callout header, so this parses `log.md` text.
// Input: { hours = 24, limit = 20, feature = null (all open features) }. An entry shows when it
// is inside the window AND among the newest `limit` (a count alone kept
// last week's entries on a quiet day, a window alone floods on a busy one).
// Dataview re-runs it when a log changes, so it needs no timer. A re-run keeps the block's last
// height until it has drawn, so the note does not jump.

const OPEN = ["proposed", "active", "blocked"];
const HEADER = /^> \[!([a-z-]+)\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2})$/;
const hours = (input && input.hours) || 24;
const limit = (input && input.limit) || 20;
const onlyFeature = input && input.feature;
const root = dv.container;
const { DateTime } = dv.luxon;
const cache = (window.__agentLiveCache = window.__agentLiveCache || {});
const key = `log:${onlyFeature || "all"}`;

if (cache[key]) root.style.minHeight = `${cache[key]}px`;

const since = DateTime.now().minus({ hours });
const plans = dv
  .pages('"features"')
  .where((p) => p.file.name === "plan" && (onlyFeature ? p.id === onlyFeature : OPEN.includes(p.status)));

const entries = [];
for (const plan of plans) {
  const text = await dv.io.load(`${plan.file.folder}/log.md`);
  if (!text) continue;
  let current = null;
  for (const line of text.split("\n")) {
    const m = line.match(HEADER);
    if (m) {
      current = { kind: m[1], ts: m[2], body: [], plan };
      entries.push(current);
    } else if (current && line.startsWith(">")) {
      current.body.push(line.replace(/^> ?/, ""));
    } else {
      current = null;
    }
  }
}

const shown = entries
  .filter((e) => DateTime.fromFormat(e.ts, "yyyy-MM-dd HH:mm") >= since)
  .sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
  .slice(0, limit);

if (!shown.length) {
  dv.paragraph(`No log entries in the last ${hours} h.`);
} else {
  const md = shown.map((e) => {
    const id = String(e.plan.id).padStart(3, "0");
    const where = onlyFeature ? "" : ` · [[${e.plan.file.path}|${id} ${e.plan.slug}]]`;
    const title = `> [!${e.kind}] ${e.ts.slice(11)}${where}`;
    return [title, ...e.body.map((l) => `> ${l}`)].join("\n");
  });
  await dv.paragraph(md.join("\n\n"));
}

// let the callouts lay out, then remember the height for the next re-run
window.setTimeout(() => {
  cache[key] = root.offsetHeight;
  root.style.minHeight = "";
}, 300);
