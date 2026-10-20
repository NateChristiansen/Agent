"""`verify record` and `verify show`: tree-keyed full pass records (verify_record.py, ADR-0041)."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    vs = sub.add_parser(
        "verify", help="tree-keyed full pass records that landing and pushing require (ADR-0041)"
    ).add_subparsers(dest="sub", required=True)
    vr = vs.add_parser(
        "record",
        help="reuse the full pass record for REV's tree, or run every verify command in an isolated checkout",
    )
    vr.add_argument("rev", help="a commit, branch or tag")
    vr.add_argument("--no-reuse", action="store_true", help="run even when a record already applies")
    vw = vs.add_parser("show", help="the record that applies to REV's tree, or why none does (exit 1)")
    vw.add_argument("rev", help="a commit, branch or tag")


def verify_record(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.verify_record(root, rev=args.rev, reuse=not args.no_reuse))


def verify_show(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.verify_show(root, rev=args.rev))


HANDLERS = {
    ("verify", "record"): verify_record,
    ("verify", "show"): verify_show,
}
