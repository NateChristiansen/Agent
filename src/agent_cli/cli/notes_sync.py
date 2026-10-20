"""`notes-sync`: the owner's side of the automatic notes sync (see ADR-0034)."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import notes_sync
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    ns = sub.add_parser("notes-sync", help="the automatic notes repo sync (ADR-0034)").add_subparsers(
        dest="sub", required=True
    )
    ns.add_parser("clear", help="lift a halt after resolving the notes repo by hand")


def notes_sync_clear(root: Path, args: argparse.Namespace, out: Output) -> int:
    text = notes_sync.clear_halt(root)
    out.emit({"result": text}, text)
    return 0


HANDLERS = {
    ("notes-sync", "clear"): notes_sync_clear,
}
