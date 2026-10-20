"""`peer`: messages between sessions, carried by this CLI rather than by either client."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    pe = sub.add_parser(
        "peer",
        help="message another session (works across Claude and Codex; see `peer list`)",
    ).add_subparsers(dest="sub", required=True)
    ps = pe.add_parser("send", help="queue a message for another live session")
    ps.add_argument("to", help="session name, id, id prefix, or tab short name like `cx 8ddb` (`peer list`)")
    ps.add_argument("message")
    ps.add_argument("--session-id")
    pi = pe.add_parser("inbox", help="read this session's messages and clear them")
    pi.add_argument("--peek", action="store_true", help="read without clearing")
    pi.add_argument("--session-id")
    pe.add_parser("list", help="sessions a message can reach, and what is pending for each")


def peer_send(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.peer_send(root, to=args.to, message=args.message, session_id=args.session_id))


def peer_inbox(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.peer_inbox(root, session_id=args.session_id, peek=args.peek))


def peer_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.peer_list(root))


HANDLERS = {
    ("peer", "send"): peer_send,
    ("peer", "inbox"): peer_inbox,
    ("peer", "list"): peer_list,
}
