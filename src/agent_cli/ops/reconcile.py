"""`reconcile`: list a feature's ledger, or settle, add and approve its items."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import features as features_mod
from .. import reconcile as reconcile_mod
from ..core import AgentError
from ._shared import Op, render_views


def _listing(f: features_mod.Feature, led: reconcile_mod.Ledger) -> str:
    if not led.on and not led.malformed:
        return f"{f.number}: nothing to reconcile"
    lines = [
        f"{f.number}: {len(led.owed)} owed, {len(led.waiting)} waiting, "
        f"{len(led.conflicts)} conflicting, {len(led.malformed)} malformed"
    ]
    for item in led.owed:
        lines.append(f"  owed {item.source} {item.item}" + (f" — {item.reason}" if item.reason else ""))
    for item in led.waiting:
        lines.append(f"  waiting {item.source} {item.item} on {(item.decision or {}).get('on')}")
    for n, cap in sorted(led.holds.items()):
        lines.append(f"  held task #{n} (cap {cap}) until approved")
    for bad in led.malformed:
        lines.append(f"  malformed {bad.get('ts', '')} {bad.get('why', '')}".rstrip())
    return "\n".join(lines)


def reconcile(
    root: Path,
    *,
    feature: str = "current",
    session_id: str | None = None,
    items: list[dict[str, Any]] | None = None,
    add: list[str] | None = None,
    approve: list[dict[str, Any]] | None = None,
) -> Op:
    """Without write arguments, a read-only listing. Decisions are grouped into one entry per
    source; a `new-task` row (with `text`) runs the capture protocol instead."""
    f = features_mod.resolve(root, feature, session_id)
    wrote: dict[str, list[dict[str, Any]]] = {"decided": [], "captured": [], "added": [], "approved": []}
    if add:
        wrote["added"].append(reconcile_mod.add_manual(root, f, add))
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in items or []:
        source = str(row.get("source") or "")
        if not source or not row.get("item") or not row.get("disposition"):
            raise AgentError("each item needs source, item and disposition")
        if row["disposition"] == "new-task":
            if not row.get("text"):
                raise AgentError(f"new-task for {source} {row['item']} needs `text`: the task to capture")
            wrote["captured"].append(
                reconcile_mod.capture(root, f, source=source, item=str(row["item"]), text=str(row["text"]))
            )
        else:
            by_source.setdefault(source, []).append(row)
    for source in sorted(by_source):
        wrote["decided"].append(reconcile_mod.decide(root, f, source, by_source[source]))
    for row in approve or []:
        if not row.get("of") or not row.get("evidence"):
            raise AgentError("each approval needs `of` (the capture) and `evidence`")
        wrote["approved"].append(
            reconcile_mod.approve(root, f, of=str(row["of"]), evidence=str(row["evidence"]))
        )
    changed = any(wrote.values())
    if changed:
        render_views(root)
    led = reconcile_mod.ledger(root, f)
    data = {"feature": f.id, **led.as_dict(), **({"wrote": wrote} if changed else {})}
    return Op(data, _listing(f, led))
