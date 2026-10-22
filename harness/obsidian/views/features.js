// Index and older notes keep this view; Python supplies every feature count.
const {DateTime} = dv.luxon;
const today = DateTime.now().startOf('day');
let snapshot;
try {
  snapshot = JSON.parse(await app.vault.adapter.read('.agent/status.json'));
} catch {
  dv.paragraph('Feature snapshot unavailable. Ask the manager to check status render.');
  return;
}
for (const status of ['active', 'blocked', 'proposed']) {
  const plans = (snapshot.features || []).filter((f) => f.status === status).sort((a, b) => a.id - b.id);
  if (!plans.length) continue;
  const group = dv.container.createDiv({cls:'agent-feature-group'});
  group.createEl('h3', {text:`Status ${status}`});
  const grid = group.createDiv({cls:'agent-feature-grid'});
  for (const plan of plans) {
    const card = grid.createDiv({cls:'agent-feature-card'});
    const title = card.createEl('strong', {cls:'agent-feature-card-title'});
    const target = `${plan.dir}/plan.md`;
    const link = title.createEl('a', {text:`${String(plan.id).padStart(3, '0')} · ${plan.title}`, cls:'internal-link'});
    link.setAttr('data-href', target);
    link.setAttr('href', target);
    link.addEventListener('click', (event) => {
      event.preventDefault();
      app.workspace.openLinkText(target, '', event.metaKey || event.ctrlKey);
    });
    card.createDiv({text:'Open tasks', cls:'agent-feature-card-label'});
    card.createDiv({text:String(plan.open_tasks)});
    if (status === 'active' && plan.open_tasks === 0 && plan.done_tasks) {
      card.createDiv({text:'active, 0 open: ready to close', cls:'agent-feature-card-cue'});
    }
    const blocker = plan.blocker || plan.blocked_since;
    if (status === 'blocked' && blocker) {
      card.createDiv({text:`blocked: ${blocker.reason} · next: ${blocker.waits_on || '—'}`, cls:'agent-feature-card-cue'});
    }
    if (plan.blocked_work?.length) {
      card.createDiv({text:`⚠ blocked with active work: ${plan.blocked_work.join('; ')}`, cls:'agent-feature-card-warning'});
    }
    if (plan.opened) {
      const opened = DateTime.fromISO(String(plan.opened));
      card.createDiv({text:'Open since', cls:'agent-feature-card-label'});
      card.createDiv({text:opened.isValid ? `${opened.toISODate()} (${Math.floor(today.diff(opened.startOf('day'), 'days').days)}d)` : String(plan.opened)});
    }
  }
}
