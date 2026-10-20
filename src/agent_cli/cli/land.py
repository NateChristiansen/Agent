"""`agent land`: move local main to a branch through a full pass record (land.py, ADR-0041)."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    lp = sub.add_parser(
        "land", help="move local main to a branch with a full pass record, as one journaled transaction"
    )
    lp.add_argument("branch", nargs="?", help="the branch or commit to land")
    lp.add_argument(
        "--strategy",
        choices=("squash", "merge", "ff"),
        help="ff for one commit on top of main, else squash by default",
    )
    lp.add_argument("--message", help="the squash or merge commit's message")
    lp.add_argument(
        "--origins",
        action="append",
        metavar="BRANCH_SHA",
        help="source '<branch> <tip-sha>' when landing a copy; repeat for each original (fetch peer first)",
    )
    lp.add_argument(
        "--with",
        dest="extra",
        action="append",
        metavar="BRANCH",
        help="land another branch in the same batch: one certified tree, all or nothing (up to 4 in all)",
    )
    which = lp.add_mutually_exclusive_group()
    which.add_argument("--recover", metavar="ID", help="finish or undo an interrupted landing")
    which.add_argument("--abort", metavar="ID", help="put a stalled landing's files back (the owner's call)")
    which.add_argument("--status", action="store_true", help="list recent landings")
    lp.add_argument(
        "--wait", type=float, default=3600.0, help="seconds to queue for the land lock (default 3600)"
    )
    lp.add_argument("--detach", action="store_true", help="run the landing in the background; print its log")
    lp.add_argument("--op", help=argparse.SUPPRESS)  # the detached child's operation id


def land(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.land(
            root,
            branch=args.branch,
            strategy=args.strategy,
            message=args.message,
            recover=args.recover,
            abort=args.abort,
            wait_s=args.wait,
            detach=args.detach,
            status=args.status,
            op=args.op,
            extra=args.extra,
            origins=args.origins,
        )
    )


HANDLERS = {("land", None): land}
