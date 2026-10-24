// The last week of daily notes: what each day cost and how much of it moved.
//
// Called from a dataviewjs block: `await dv.view("harness/obsidian/views/journal", { days: 7 })`.
// It replaced `bases/journal.base#Week` on the Dashboard, whose six columns scrolled sideways
// past the readable line width while every other section stayed inside it. Same fixed-layout
// table the live views use: percentage widths, wrapping cells, no horizontal scroll. Index.md reuses this view over a longer date window.
//
// Reads frontmatter only, written by `bin/agent journal` (`src/agent_cli/journal/block.py`):
// `spend_usd` dispatched workers, `manager_usd` estimates for the owner's own sessions, `dispatches`, `session_tokens`. Dataview re-runs the block when a note changes, so it
// needs no timer.

const days = (input && input.days) || 7;
const { DateTime } = dv.luxon;
const root = dv.container;

function usd(value, complete = true) {
  const n = Number(value || 0);
  if (!complete) return `$${n.toFixed(2)}+`;
  return n ? `$${n.toFixed(2)}` : "—";
}

function tokens(value) {
  const n = Number(value || 0);
  if (!n) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

// `date` is an unquoted YAML date, so Dataview hands over a DateTime already; a note someone
// wrote by hand may carry a string instead.
function when(page) {
  const value = page.date;
  if (!value) return null;
  const dt = typeof value === "string" ? DateTime.fromISO(value) : DateTime.fromISO(value.toISO());
  return dt.isValid ? dt : null;
}

const since = DateTime.now().startOf("day").minus({ days: days - 1 });
const rows = dv
  .pages('"journal"')
  .map((p) => ({ page: p, dt: when(p) }))
  .where((r) => r.dt && r.dt >= since)
  .sort((r) => r.dt.toMillis(), "desc");

if (!rows.length) {
  dv.paragraph(`No journal notes in the last ${days} days.`);
} else {
  const table = root.createEl("table", { cls: "agent-live-table" });
  table.style.width = "100%";
  table.style.tableLayout = "fixed";
  const colgroup = table.createEl("colgroup");
  [30, 18, 18, 16, 18].forEach((w) => (colgroup.createEl("col").style.width = `${w}%`));
  const head = table.createEl("thead").createEl("tr");
  ["Day", "Workers", "Manager", "Dispatches", "Tokens"].forEach((h) => head.createEl("th", { text: h }));
  const body = table.createEl("tbody");

  let workers = 0;
  let manager = 0;
  let workersComplete = true;
  let managerComplete = true;
  let dispatches = 0;
  let volume = 0;
  for (const { page, dt } of rows) {
    workers += Number(page.spend_usd || 0);
    manager += Number(page.manager_usd || 0);
    workersComplete &&= page.spend_complete !== false;
    managerComplete &&= page.manager_complete !== false;
    dispatches += Number(page.dispatches || 0);
    volume += Number(page.session_tokens || 0);
    const tr = body.createEl("tr");
    const day = tr.createEl("td");
    day.style.overflowWrap = "anywhere";
    const a = day.createEl("a", { text: dt.toFormat("ccc d LLL"), cls: "internal-link" });
    a.setAttr("data-href", page.file.path);
    a.setAttr("href", page.file.path);
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      app.workspace.openLinkText(page.file.path, "", ev.metaKey || ev.ctrlKey);
    });
    // `session_models` is the column that made the old table scroll. It is one hover away.
    const cells = [usd(page.spend_usd, page.spend_complete !== false), usd(page.manager_usd, page.manager_complete !== false), page.dispatches || "—", tokens(page.session_tokens)];
    cells.forEach((text, i) => {
      const td = tr.createEl("td", { text: String(text) });
      td.style.overflowWrap = "anywhere";
      if (i === 3 && page.session_models) td.setAttr("title", String(page.session_models));
    });
  }

  const foot = table.createEl("tfoot").createEl("tr");
  [`${rows.length} days`, usd(workers, workersComplete), usd(manager, managerComplete), dispatches || "—", tokens(volume)].forEach((text) => {
    const td = foot.createEl("td", { text: String(text) });
    td.style.fontWeight = "600";
  });
}
