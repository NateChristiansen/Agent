"""Register repository list, status and sync commands together under repos."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # repos
    r = sub.add_parser("repos", help="repo registry").add_subparsers(dest="sub", required=True)
    r.add_parser("list", help="list registered repos")
    r.add_parser("validate", help="validate config, including unknown keys and spelling hints")
    rs = r.add_parser("status", help="git status per repo")
    rs.add_argument("name", nargs="?")
    rs.add_argument("--fetch", action="store_true")
    ry = r.add_parser("sync", help="clone missing repos, repair worktrees, fast-forward main")
    ry.add_argument("name", nargs="?")
    ry.add_argument("--no-clone", action="store_true")
    ry.add_argument("--fetch", action="store_true")
    ry.add_argument(
        "--pull",
        action="store_true",
        help="fetch, then fast-forward the default branch; skips a dirty or non-default checkout",
    )
    ry.add_argument(
        "--harness",
        action="store_true",
        help="fetch main from the pinned harness.sync_url, then fast-forward the harness's own main "
        "to it (ADR-0041); refuses when main has commits it lacks",
    )


def repos_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.repos_list(root))


def repos_validate(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.repos_validate(root))


def repos_status(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.repos_status(root, name=args.name, fetch=args.fetch))


def repos_sync(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.repos_sync(
            root,
            name=args.name,
            no_clone=args.no_clone,
            fetch=args.fetch,
            pull=args.pull,
            harness=args.harness,
        )
    )


HANDLERS = {
    ("repos", "list"): repos_list,
    ("repos", "validate"): repos_validate,
    ("repos", "status"): repos_status,
    ("repos", "sync"): repos_sync,
}
