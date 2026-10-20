"""`session`: the session-to-feature mapping records."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # session
    se = sub.add_parser("session", help="session ↔ feature mapping").add_subparsers(dest="sub", required=True)
    sg = se.add_parser("get", help="print the record for a session")
    sg.add_argument("--session-id")
    sq = se.add_parser("question", help="mark the session as waiting on the owner's answer (tab shows ✋)")
    sq.add_argument("--session-id")
    sq.add_argument("--summary", help="a short line naming what was asked")
    ss = se.add_parser("set", help="upsert a session record")
    ss.add_argument("--session-id", required=True)
    ss.add_argument("--feature")
    ss.add_argument("--cwd")
    ss.add_argument("--name")
    sp = se.add_parser("prune", help="delete stale session records (never a live one)")
    sp.add_argument("--days", type=int, default=14, help="remove records older than this")
    sp.add_argument("--dead", action="store_true", help="remove every record whose process is gone")
    sr = se.add_parser("rm", help="delete one session record and the by-pid entries naming it")
    sr.add_argument("session_id")
    sr.add_argument("--force", action="store_true", help="remove it even if its process is live")
    se.add_parser("list", help="every session record as LIVE or DEAD")
    sx = se.add_parser(
        "export", help="copy .agent/ evidence (sessions, raw captures, tabs, peers) out of this clone"
    )
    sx.add_argument("dest", help="a new or empty folder outside this clone")
    sx.add_argument("--feature", help="limit raw captures to this feature")


def session_get(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.session_get(root, session_id=args.session_id))


def session_question(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.owner_question(root, session_id=args.session_id, summary=args.summary))


def session_set(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.session_set(root, session_id=args.session_id, feature=args.feature, cwd=args.cwd, name=args.name)
    )


def session_prune(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.session_prune(root, days=args.days, dead=args.dead))


def session_rm(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.session_rm(root, session_id=args.session_id, force=args.force))


def session_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.session_list(root))


def session_export(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.session_export(root, dest=args.dest, feature=args.feature))


HANDLERS = {
    ("session", "get"): session_get,
    ("session", "question"): session_question,
    ("session", "set"): session_set,
    ("session", "prune"): session_prune,
    ("session", "rm"): session_rm,
    ("session", "list"): session_list,
    ("session", "export"): session_export,
}
