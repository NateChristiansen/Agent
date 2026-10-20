// Compatibility for older notes. The Dashboard uses keyed plugin rows now.
let snapshot;
try {
  snapshot = JSON.parse(await app.vault.adapter.read('.agent/status.json'));
} catch {
  dv.paragraph('Task snapshot unavailable. Ask the manager to check status render.');
  return;
}
let shown = 0;
let unknown = false;
for (const plan of snapshot.features || []) {
  if (!['active', 'blocked', 'proposed'].includes(plan.status)) continue;
  if (plan.open_tasks && !Array.isArray(plan.open_task_items)) { unknown = true; continue; }
  const tasks = plan.open_task_items || [];
  if (!tasks.length) continue;
  shown += tasks.length;
  dv.paragraph(`[[${plan.dir}/plan|${String(plan.id).padStart(3, '0')} ${plan.slug}]] · ${plan.status}`);
  dv.list(tasks.map((t) => `#${t.index} ${t.text}`));
}
if (unknown) dv.paragraph('Task text unavailable. Ask the manager to render status.');
else if (!shown) dv.paragraph('No open tasks.');
