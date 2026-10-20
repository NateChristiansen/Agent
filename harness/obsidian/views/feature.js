// A feature's header: task counts, dispatches and spend, and the last log entry.
// Embedded at the top of each plan.md:
//   await dv.view("harness/obsidian/views/feature")
//
// Reads only indexed markdown (the plan's own `## Tasks`, handoff frontmatter, log.md), so
// Dataview re-runs it when any of them changes and it needs no timer.

const HEADER = /^> \[!([a-z-]+)\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2})$/;
const page = dv.current();
const folder = page.file.folder;

// Counted as `features.parse_tasks` counts them: done is `completed` (only `x`/`X`),
// superseded (`[-]`, status "-") is neither done nor open and stays out of the total. Only tasks
// directly under a `Tasks` heading (`Tasks` or `Tasks …`) count; a subsection ends the run.
const isTasks = (s) => s === "Tasks" || s.startsWith("Tasks ");
const all = page.file.tasks.where((t) => t.section && isTasks(t.section.subpath) && !t.parent && t.text.trim());
const superseded = all.where((t) => t.status === "-").length;
const tasks = all.where((t) => t.status !== "-");
const done = tasks.where((t) => t.completed).length;

const handoffs = dv.pages(`"${folder}/handoffs"`).where((h) => h.file.tags && h.file.tags.includes("#handoff"));
const spend = handoffs.array().reduce((sum, h) => sum + Number(h.cost_usd || 0), 0);
const running = handoffs.where((h) => h.status === "running" || h.status === "queued").length;
const waiting = handoffs.where((h) => h.live === true && h.status !== "running" && h.status !== "queued").length;

let last = null;
const log = await dv.io.load(`${folder}/log.md`);
if (log) {
  const lines = log.split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = lines[i].match(HEADER);
    if (m) {
      const body = [];
      for (let j = i + 1; j < lines.length && lines[j].startsWith(">"); j++) body.push(lines[j].replace(/^> ?/, ""));
      const text = body.join(" ").replace(/\s+/g, " ").trim();
      last = { kind: m[1], ts: m[2], text: text.length > 200 ? text.slice(0, 197) + "…" : text };
      break;
    }
  }
}

const parts = [
  `**${page.status}**`,
  `tasks ${done}/${tasks.length} done` + (superseded ? `, ${superseded} superseded` : ""),
  `${handoffs.length} dispatch${handoffs.length === 1 ? "" : "es"} · $${spend.toFixed(2)}`,
];
if (running) parts.push(`⚙ ${running} running`);
if (waiting) parts.push(`⏸ ${waiting} waiting on you`);
dv.paragraph(parts.join(" · "));
if (last) dv.paragraph(`last log: \`${last.ts}\` **${last.kind}** — ${last.text}`);
