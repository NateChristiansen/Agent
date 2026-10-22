// Plan handoffs are indexed records; dispatch lifecycle operations never rewrite plan.md.
const page = dv.current();
const folder = page.file.folder;
const handoffs = dv.pages(`"${folder}/handoffs"`)
  .where((h) => h.file.tags && h.file.tags.includes("#handoff"))
  .sort((h) => h.id || h.file.name, "asc");
dv.table(["Dispatch", "Repo", "Status", "Cost"], handoffs.map((h) => [
  h.file.link, h.repo, h.status, `$${Number(h.cost_usd || 0).toFixed(2)}`,
]));

// A request's review rounds: one row per pinned review dispatch, read from the synced handoff
// provenance, never from runtime state. A handoff written before those fields shows a dash.
if (page.record_kind === "request") {
  const rounds = handoffs.where((h) => h.row === "review" || h.review_checkout || h.review_target);
  const shown = (v, n) => (v ? String(v).slice(0, n) : "—");
  dv.paragraph("**Review rounds**");
  dv.table(["Round", "Dispatch", "Target", "Checkout", "Compare", "PR", "Status"], rounds.map((h, i) => [
    i + 1, h.file.link, shown(h.review_target, 80), shown(h.review_checkout, 12),
    shown(h.review_compare, 12), shown(h.review_pr, 80), h.status,
  ]));
}
