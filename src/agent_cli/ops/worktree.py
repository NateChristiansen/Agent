"""`worktree new|list|remove|link`: a checkout per manager over one shared state."""

from __future__ import annotations

from pathlib import Path

from .. import worktree as worktree_mod
from ..core import table
from ..records.narrow import as_list
from ._shared import Op


def worktree_new(
    root: Path,
    *,
    name: str,
    base: str = "main",
    sync: bool = True,
    feature: str | None = None,
    session_id: str | None = None,
) -> Op:
    result = worktree_mod.create(root, name, base=base, sync=sync, feature=feature, session=session_id)
    lines = [f"{result['path']}  ({result['branch']})", *(f"  {step}" for step in result["steps"])]
    if differs := result.get("branch_differs"):
        lines.append(
            f"  note: {result['branch']} is at {differs['branch_tip'][:12]}, not at base {differs['base']}"
        )
    lines.append(f"  open it with: cd {result['path']}")
    return Op(result, "\n".join(lines))


def worktree_list(root: Path) -> Op:
    rows = worktree_mod.list_all(root)
    if not rows:
        return Op([], "no worktrees; the main checkout is the only one")
    return Op(
        rows,
        table(
            [[r["path"], r["branch"] or "—", "yes" if r["manager"] else "no", r["state"]] for r in rows],
            ["path", "branch", "manager", "state"],
        ),
    )


def worktree_remove(root: Path, *, name: str, force: bool = False) -> Op:
    result = worktree_mod.remove(root, name, force=force)
    return Op(result, f"removed {result['path']}; its branch is kept")


def worktree_restore(root: Path, *, name: str, head: str, apply: bool = False, sync: bool = True) -> Op:
    r = worktree_mod.restore(root, name, head=head, apply=apply, sync=sync)
    if r.get("already"):
        return Op(r, f"{r['path']} already present at {head}")
    verb = "restored" if r["restored"] else "kept" if r["holds"] else "preview"
    lines = [f"{verb}: {r['path']} on {r['branch']} at {head}"]
    lines += [f"  hold: {h}" for h in r["holds"]]
    lines += [f"  {s}" for s in as_list(r.get("steps"))]
    lines += [f"  warning: {w}" for w in as_list(r.get("warnings"))]
    return Op(r, "\n".join(lines))


def worktree_reconcile(root: Path, *, path: str, apply: bool = False) -> Op:
    """Manager-facing name for the one reconcile route dispatch records share."""
    from .dispatch import dispatch_reconcile

    return dispatch_reconcile(root, path=path, apply=apply)


def worktree_link(root: Path) -> Op:
    """Repair this worktree's `.agent` link. Separate from `new` because the link is the one
    step whose absence is silent: a worktree with its own `.agent` works, and coordinates with
    nobody."""
    result = worktree_mod.link_state(root)
    if not result["linked"]:
        return Op(result, f"already shares {result['target']}")
    return Op(result, f"{result['path']} -> {result['target']}")
