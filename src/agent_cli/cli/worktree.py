"""`worktree`: a checkout per manager, sharing one `.agent`."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    wt = sub.add_parser(
        "worktree",
        help="a checkout per manager session, over one shared .agent",
    ).add_subparsers(dest="sub", required=True)

    new = wt.add_parser("new", help="add a manager worktree on agent/<name> and wire it up")
    new.add_argument("name", help="one word; the branch is agent/<name>")
    new.add_argument("--base", default="main", help="the ref to branch from (default: main)")
    new.add_argument(
        "--no-sync",
        action="store_true",
        help="skip `uv sync`; the worktree needs its own .venv before bin/agent runs there",
    )

    wt.add_parser("list", help="every worktree, and whether its .agent reaches the shared state")

    rm = wt.add_parser("remove", help="remove a worktree; its branch is kept")
    rm.add_argument("name")
    rm.add_argument("--force", action="store_true", help="remove even with uncommitted changes")

    wt.add_parser("link", help="point this worktree's .agent at the main checkout's")


def worktree_new(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.worktree_new(root, name=args.name, base=args.base, sync=not args.no_sync))


def worktree_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.worktree_list(root))


def worktree_remove(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.worktree_remove(root, name=args.name, force=args.force))


def worktree_link(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.worktree_link(root))


HANDLERS = {
    ("worktree", "new"): worktree_new,
    ("worktree", "list"): worktree_list,
    ("worktree", "remove"): worktree_remove,
    ("worktree", "link"): worktree_link,
}
