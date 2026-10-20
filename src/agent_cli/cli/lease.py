"""`lease`: advisory area locks under .agent/leases."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # lease
    le = sub.add_parser("lease", help="advisory area locks (.agent/leases)").add_subparsers(
        dest="sub", required=True
    )
    let_ = le.add_parser("take", help="take (or, held by this session, touch) a lease")
    let_.add_argument("area")
    let_.add_argument("--note")
    let_.add_argument("--session-id")
    lrl = le.add_parser("release", help="release a lease this session holds; a no-op otherwise")
    lrl.add_argument("area")
    lrl.add_argument("--session-id")
    le.add_parser("list", help="every lease as live, resting, idle, dead or unverified")
    lc = le.add_parser(
        "clear",
        help="free every lease a stuck session holds; asks for --yes if it was active in the last 30 minutes",
    )
    lc.add_argument("holder", help="the session id or name (its peer address)")
    lc.add_argument("--yes", action="store_true", help="clear even though the session was active recently")
    lw = le.add_parser(
        "wait", help="poll until an area frees (or this session already holds it), then take it"
    )
    lw.add_argument("area")
    lw.add_argument("--note")
    lw.add_argument("--timeout", type=float, default=120)
    lw.add_argument("--session-id")
    lw.add_argument(
        "--ask",
        metavar="WHY",
        help="message the current holder that you are waiting, then wait: say why you need it "
        "and which files, so the holder can release or carve out",
    )


def lease_take(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.lease_take(root, area=args.area, note=args.note, session_id=args.session_id))


def lease_release(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.lease_release(root, area=args.area, session_id=args.session_id))


def lease_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.lease_list(root))


def lease_clear(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.lease_clear(root, who=args.holder, confirm=args.yes))


def lease_wait(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.lease_wait(
            root,
            area=args.area,
            note=args.note,
            timeout=args.timeout,
            session_id=args.session_id,
            ask=args.ask,
        )
    )


HANDLERS = {
    ("lease", "take"): lease_take,
    ("lease", "release"): lease_release,
    ("lease", "list"): lease_list,
    ("lease", "wait"): lease_wait,
    ("lease", "clear"): lease_clear,
}
