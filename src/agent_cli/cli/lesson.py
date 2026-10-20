"""`lesson`: the queue of findings with no open feature to hold them."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import lessons
from ..core import Output
from ..ops import lessons as lesson_ops
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    le = sub.add_parser(
        "lesson", help="the lesson queue: findings from closed features, retros, or no feature"
    ).add_subparsers(dest="sub", required=True)
    la = le.add_parser("add", help="queue a finding that has no open feature to hold it")
    la.add_argument("title")
    la.add_argument("--scope", choices=lessons.SCOPES, help="accepted and ignored: there is one local queue")
    la.add_argument("--kind", default="lesson", choices=lessons.KINDS)
    la.add_argument("--source", default="", help='where it came from: "a retro", a feature, or "none"')
    la.add_argument("--evidence", required=True, help="the symptom or owner quote, self-contained")
    la.add_argument("--home", default="", help="the proposed home: a skill reference, a hook, a feature")
    ll = le.add_parser("list", help="open entries in the local queue")
    ll.add_argument("--scope", choices=(*lessons.SCOPES, "all"), help="accepted and ignored")
    lr = le.add_parser("resolve", help="close an entry with its disposition")
    lr.add_argument("id")
    lr.add_argument("--disposition", required=True, choices=lessons.DISPOSITIONS)
    lr.add_argument(
        "--note", required=True, help="the task, commit, file, issue URL, or the reason for dropping"
    )
    ld = le.add_parser("draft-check", help="check an upstream issue draft: upstream repo, fallback URL")
    ld.add_argument("--title", required=True, help="the issue title, starting with [lesson]")
    ld.add_argument("--body-file", required=True, help="the issue body, ending with `harness-lesson: <id>`")


def lesson_add(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        lesson_ops.lesson_add(
            root,
            title=args.title,
            scope=args.scope,
            kind=args.kind,
            source=args.source,
            evidence=args.evidence,
            home=args.home,
        )
    )


def lesson_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(lesson_ops.lesson_list(root, scope=args.scope))


def lesson_resolve(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        lesson_ops.lesson_resolve(root, entry_id=args.id, disposition=args.disposition, note=args.note)
    )


def lesson_draft_check(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(lesson_ops.lesson_draft_check(root, title=args.title, body_file=args.body_file))


HANDLERS = {
    ("lesson", "add"): lesson_add,
    ("lesson", "list"): lesson_list,
    ("lesson", "resolve"): lesson_resolve,
    ("lesson", "draft-check"): lesson_draft_check,
}
