"""`scratch`: the .agent/scratch drafts directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # scratch
    sc = sub.add_parser(
        "scratch",
        help="`.agent/scratch/`: plan-mode drafts, dispatch work orders, journal drafts (outside the vault)",
    ).add_subparsers(dest="sub", required=True)
    sc.add_parser("path", help="print the scratch root (ensures it exists)")


def scratch_path(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.scratch_path(root))


HANDLERS = {
    ("scratch", "path"): scratch_path,
}
