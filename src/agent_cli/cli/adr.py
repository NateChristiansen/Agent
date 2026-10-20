"""`adr`: architecture decision records."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import adrs as adrs_mod
from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # adr
    a = sub.add_parser("adr", help="architecture decision records").add_subparsers(dest="sub", required=True)
    an = a.add_parser("new", help="create a private decision; use --scope harness for a public ADR")
    an.add_argument("slug")
    an.add_argument("--title", required=True)
    an.add_argument("--feature")
    an.add_argument("--status", default="proposed", choices=adrs_mod.STATUSES)
    an.add_argument("--supersedes", type=int)
    an.add_argument("--session-id")
    an.add_argument("--scope", default="notes", choices=("notes", "harness"))


def adr_new(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.adr_new(
            root,
            slug=args.slug,
            title=args.title,
            feature=args.feature,
            status=args.status,
            supersedes=args.supersedes,
            session_id=args.session_id,
            scope=args.scope,
        )
    )


HANDLERS = {
    ("adr", "new"): adr_new,
}
