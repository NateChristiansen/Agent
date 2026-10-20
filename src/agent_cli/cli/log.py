"""`log`: append a hindsight entry to a feature's log.md."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import log as log_mod
from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # log
    lg = sub.add_parser("log", help="append a log entry")
    lg.add_argument("feature", help="1, 001, 001-slug, slug, or current")
    lg.add_argument("text", nargs="?")
    lg.add_argument("--kind", default="note", choices=log_mod.MODEL_KINDS)
    lg.add_argument("--stdin", action="store_true", help="read the entry text from stdin")
    lg.add_argument("--session-id")


def log_cmd(root: Path, args: argparse.Namespace, out: Output) -> int:
    text = sys.stdin.read() if args.stdin else (args.text or "")
    return out.done(
        ops.log(root, kind=args.kind, text=text, feature=args.feature, session_id=args.session_id)
    )


HANDLERS = {
    ("log", None): log_cmd,
}
