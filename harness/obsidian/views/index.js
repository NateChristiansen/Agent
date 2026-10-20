// Compact navigation entries for Index.md. Each entry links to the full note; the generated
// Bases views remain available when their detailed columns are useful.

const section = input && input.section;
const kind = input && input.kind;
const { DateTime } = dv.luxon;
const root = dv.container;

function date(value) {
  if (!value) return null;
  const parsed = typeof value === "string" ? DateTime.fromISO(value) : value;
  return parsed && parsed.isValid ? parsed : null;
}

function link(container, page, label) {
  const anchor = container.createEl("a", { text: label, cls: "internal-link" });
  anchor.setAttr("data-href", page.file.path);
  anchor.setAttr("href", page.file.path);
  anchor.addEventListener("click", (event) => {
    event.preventDefault();
    app.workspace.openLinkText(page.file.path, "", event.metaKey || event.ctrlKey);
  });
}

function entry(page, label, details) {
  const row = root.createDiv({ cls: "agent-index-entry" });
  link(row.createDiv({ cls: "agent-index-entry-title" }), page, label);
  if (details) row.createDiv({ text: details, cls: "agent-index-entry-details" });
}

function showEmpty(message) {
  root.createDiv({ text: message, cls: "agent-index-empty" });
}

if (section === "closed") {
  const pages = dv
    .pages('"features"')
    .where((p) => p.file.name === "plan" && ["done", "abandoned"].includes(p.status))
    .sort((p) => date(p.closed)?.toMillis() || 0, "desc");
  if (!pages.length) showEmpty("No closed features.");
  for (const page of pages) {
    const closed = date(page.closed);
    entry(page, `${String(page.id).padStart(3, "0")} · ${page.title}`, [page.status, closed?.toISODate()].filter(Boolean).join(" · "));
  }
} else if (section === "adrs") {
  const pages = dv.pages('"adrs"').where((p) => typeof p.id === "number").sort((p) => p.id, "desc");
  if (!pages.length) showEmpty("No decisions yet.");
  for (const page of pages) {
    entry(page, `ADR-${String(page.id).padStart(4, "0")} · ${page.title}`, [page.status, date(page.date)?.toISODate()].filter(Boolean).join(" · "));
  }
} else if (section === "docs" && ["retro", "design", "audit", "qa-plan"].includes(kind)) {
  const pages = dv
    .pages('"features"')
    .where((p) => p.file.name === kind)
    .sort((p) => date(p.date)?.toMillis() || 0, "desc");
  if (!pages.length) showEmpty(`No ${kind} notes yet.`);
  for (const page of pages) {
    const feature = page.file.folder.split("/").pop();
    const label = `${feature} · ${kind === "qa-plan" ? "QA plan" : kind}`;
    entry(page, label, date(page.date)?.toISODate() || "");
  }
} else if (section === "handoffs") {
  const pages = dv
    .pages('"features"')
    .where((p) => p.file.folder.endsWith("/handoffs"))
    .sort((p) => date(p.finished || p.started)?.toMillis() || 0, "desc");
  if (!pages.length) showEmpty("No handoffs yet.");
  for (const page of pages) {
    const cost = Number(page.cost_usd || 0);
    const when = date(page.finished || page.started)?.toISODate();
    entry(page, `${page.id} · ${page.repo}`, [page.status, `$${cost.toFixed(2)}`, when].filter(Boolean).join(" · "));
  }
} else {
  showEmpty("Unknown Index view.");
}
