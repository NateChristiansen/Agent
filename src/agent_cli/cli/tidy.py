"""`agent tidy`: inspect finished checkouts, or apply their safety checks."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import tidy
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    parser = sub.add_parser("tidy", help="finished checkouts and branches; dry-run by default")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list without changing anything (default)")
    mode.add_argument("--apply", action="store_true", help="remove safe trees and resolved agent/* branches")
    parser.add_argument("--feature", type=int, help="only this feature's checkouts and branches")


def tidy_cmd(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(tidy.operation(root, apply=args.apply, feature=args.feature))


HANDLERS = {("tidy", None): tidy_cmd}
