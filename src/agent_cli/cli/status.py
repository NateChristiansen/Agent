"""`status`: render and check generated files."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # status
    s = sub.add_parser("status", help="generated vault views / ADR indexes / status.json").add_subparsers(
        dest="sub", required=True
    )
    s.add_parser("render", help="regenerate the generated files")
    s.add_parser("check", help="exit 1 if the generated files are stale")


def status_render(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.status_render(root))


def status_check(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.status_check(root))


HANDLERS = {
    ("status", "render"): status_render,
    ("status", "check"): status_check,
}
