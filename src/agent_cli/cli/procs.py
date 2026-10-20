"""`agent procs list|keep|unkeep|reap`: the processes the harness launched, who owns each, and the
owner's keep-alives.

`procs list` is the `harness` MCP tool's `procs_list` action. `keep`, `unkeep` and `reap` are
person-only: a manager asked to keep or reap something gives the owner the command.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from .. import procs as procs_mod
from ..core import Output
from ..ops import Op
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    p = sub.add_parser(
        "procs", help="processes the harness launched: who owns each, keep-alives, orphans"
    ).add_subparsers(dest="sub", required=True)
    p.add_parser("list", help="every harness-owned process, by owner, with orphans marked")
    pk = p.add_parser("keep", help="let a process outlive its owner's reap (a dev server still in use)")
    pk.add_argument("pid", type=int)
    pk.add_argument("--note", default="", help="why it is kept, shown in `procs list`")
    pu = p.add_parser("unkeep", help="let a kept process be reaped with its owner again")
    pu.add_argument("pid", type=int)
    pr = p.add_parser("reap", help="kill orphans: escaped, tagged, owner gone, not kept")
    pr.add_argument("--dry-run", action="store_true", help="name what would be reaped and stop")


def procs_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.procs_list(root))


def procs_keep(root: Path, args: argparse.Namespace, out: Output) -> int:
    result = procs_mod.keep(root, args.pid, args.note)
    if not result["kept"]:
        return out.done(Op(result, f"not kept: pid {args.pid} is not running"))
    return out.done(Op(result, f"kept {args.pid}: {result['command'][:80]}"))


def procs_unkeep(root: Path, args: argparse.Namespace, out: Output) -> int:
    result = procs_mod.unkeep(root, args.pid)
    text = (
        f"{args.pid} will be reaped with its owner again" if result["unkept"] else f"{args.pid} was not kept"
    )
    return out.done(Op(result, text))


def procs_reap(root: Path, args: argparse.Namespace, out: Output) -> int:
    snapshot = procs_mod.snapshot()
    orphan_rows = procs_mod.orphans(root, snapshot)
    if not orphan_rows:
        return out.done(Op({"orphans": []}, "no orphans"))
    names = ", ".join(f"{r['pid']} ({r['owner']})" for r in orphan_rows)
    if args.dry_run:
        return out.done(Op({"orphans": orphan_rows}, f"would reap: {names}"))
    targets = [p for p in snapshot if p.pid in {r["pid"] for r in orphan_rows}]
    result = procs_mod.reap(root, targets)
    return out.done(Op({"orphans": orphan_rows, **result}, f"reaped: {names}"))


HANDLERS = {
    ("procs", "list"): procs_list,
    ("procs", "keep"): procs_keep,
    ("procs", "unkeep"): procs_unkeep,
    ("procs", "reap"): procs_reap,
}
