// Plan handoffs are indexed records; dispatch lifecycle operations never rewrite plan.md.
const folder = dv.current().file.folder;
const handoffs = dv.pages(`"${folder}/handoffs"`)
  .where((h) => h.file.tags && h.file.tags.includes("#handoff"))
  .sort((h) => h.id || h.file.name, "asc");
dv.table(["Dispatch", "Repo", "Status", "Cost"], handoffs.map((h) => [
  h.file.link, h.repo, h.status, `$${Number(h.cost_usd || 0).toFixed(2)}`,
]));
