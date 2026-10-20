"""`journal`: the day's material, the missing-days list, the generated block and its numbers."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # journal
    j = sub.add_parser("journal", help="daily material").add_subparsers(dest="sub", required=True)
    jc = j.add_parser("collect", help="assemble one day's logs, captures, ADRs, git activity")
    jc.add_argument("date", nargs="?", help="YYYY-MM-DD (default today)")
    jc.add_argument("--write", action="store_true", help="save to .agent/journal/<date>.collect.md|json")
    j.add_parser("missing", help="days since 2026-09-03 with activity and no journal block yet")
    jw = j.add_parser(
        "write", help="replace a day's generated block from a file (/journal's write); Notes untouched"
    )
    jw.add_argument("date", help="YYYY-MM-DD")
    jw.add_argument("--file", required=True, help="the block text, no markers")


def journal_collect(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.journal_collect(root, date=args.date, write=args.write, as_json=out.as_json))


def journal_missing(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.journal_missing(root))


def journal_write(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.journal_write(root, date=args.date, file=args.file))


HANDLERS = {
    ("journal", "collect"): journal_collect,
    ("journal", "missing"): journal_missing,
    ("journal", "write"): journal_write,
}
