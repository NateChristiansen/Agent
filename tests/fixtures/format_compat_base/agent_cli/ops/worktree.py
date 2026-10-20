# ruff: noqa
from __future__ import annotations
from pathlib import Path
from .. import worktree as worktree_mod
from ..core import table
from ._shared import Op


def worktree_new(root: Path, *, name: str, base: str = "main", sync: bool = True) -> Op:
    result = worktree_mod.create(root, name, base=base, sync=sync)
    lines = [f"{result['path']}  ({result['branch']})", *(f"  {step}" for step in result["steps"])]
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


def worktree_link(root: Path) -> Op:
    result = worktree_mod.link_state(root)
    if not result["linked"]:
        return Op(result, f"already shares {result['target']}")
    return Op(result, f"{result['path']} -> {result['target']}")
