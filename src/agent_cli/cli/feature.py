"""`feature`, `task` and `raw`: the feature lifecycle front end.

`raw` is a separate `add_raw_parser` because the top-level commands must be registered in
the order they had in the old single-file cli, and `raw` sat between `scratch` and
`capture`. Moving it next to `feature` would change `agent --help`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import features as features_mod
from .. import ops
from ..core import EXIT_OK, Output
from ..records.narrow import as_list
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # feature
    f = sub.add_parser("feature", help="feature lifecycle").add_subparsers(dest="sub", required=True)
    fn = f.add_parser("new", help="create features/NNN-slug")
    fn.add_argument("slug")
    fn.add_argument("--title", required=True)
    fn.add_argument("--status", default="active", choices=features_mod.OPEN_STATUSES)
    fn.add_argument(
        "--ticket", nargs="*", action="extend", default=[], help="ticket id, e.g. TK#123 or JIRA-45"
    )
    fn.add_argument("--repo", nargs="*", action="extend", default=[])
    fn.add_argument("--tag", nargs="*", action="extend", default=[])
    fn.add_argument("--use", action="store_true", help="map the current session to the new feature")
    fn.add_argument("--session-id")
    fl = f.add_parser("list", help="list features")
    fl.add_argument("--status", nargs="*", action="extend", choices=features_mod.STATUSES)
    fl.add_argument("--all", action="store_true", help="include closed features")
    fl.add_argument("--include-requests", action="store_true", help="include request records")
    fs = f.add_parser("show", help="show one feature")
    fs.add_argument("feature")
    fs.add_argument("--session-id")
    fu = f.add_parser("use", help="map a session to a feature")
    fu.add_argument("feature")
    fu.add_argument("--session-id")
    fu.add_argument("--switch", action="store_true", help="owner-requested ownership switch")
    fc = f.add_parser("current", help="print the feature mapped to this session")
    fc.add_argument("--session-id")
    fe = f.add_parser("set", help="edit frontmatter fields")
    fe.add_argument("feature")
    fe.add_argument("--session-id")
    fe.add_argument("--status", choices=features_mod.STATUSES)
    fe.add_argument("--title")
    fe.add_argument("--add-ticket", nargs="*", action="extend")
    fe.add_argument("--remove-ticket", nargs="*", action="extend")
    fe.add_argument("--add-repo", nargs="*", action="extend")
    fe.add_argument("--remove-repo", nargs="*", action="extend")
    fe.add_argument("--add-adr", type=int, nargs="*", action="extend")
    fe.add_argument("--remove-adr", type=int, nargs="*", action="extend")
    fe.add_argument("--add-tag", nargs="*", action="extend")
    fe.add_argument("--remove-tag", nargs="*", action="extend")
    fe.add_argument("--reason", help="why the status changes; logged with it")
    fx = f.add_parser("close", help="close a feature")
    fx.add_argument("feature")
    fx.add_argument("--session-id")
    fx.add_argument("--status", default="done", choices=features_mod.TERMINAL_STATUSES)
    fx.add_argument("--note", help="log a closing decision entry")

    # task
    t = sub.add_parser("task", help="checkboxes under ## Tasks in a feature's plan.md").add_subparsers(
        dest="sub", required=True
    )
    tl = t.add_parser("list", help="list tasks with their numbers")
    tl.add_argument("feature", nargs="?", default="current")
    tl.add_argument("--open", action="store_true", help="only unchecked tasks")
    tl.add_argument("--session-id")
    td = t.add_parser("done", help="tick a task by number or unique substring")
    td.add_argument("feature", help="feature ref, or `current`")
    td.add_argument("task", help="task number or a substring of its text")
    td.add_argument("--note", help="appended in parentheses, e.g. a commit sha or 'via /intake'")
    td.add_argument("--session-id")
    tu = t.add_parser("undo", help="untick a task")
    tu.add_argument("feature")
    tu.add_argument("task")
    tu.add_argument("--session-id")
    ta = t.add_parser("add", help="append a task to ## Tasks")
    ta.add_argument("feature")
    ta.add_argument("text")
    ta.add_argument("--session-id")
    tsu = t.add_parser("supersede", help="mark a task superseded ([-]) with a reason and a link")
    tsu.add_argument("feature")
    tsu.add_argument("task", help="task number or a substring of its text")
    tsu.add_argument("--reason", required=True, help="why it is no longer wanted as written")
    tsu.add_argument("--link", required=True, help="where it went: a commit, NNN #k, a feature or a URL")
    tsu.add_argument("--session-id")
    tc = t.add_parser("check", help="read-only: evidence that open tasks may already be done or stale")
    tc.add_argument("feature", nargs="?", default="current")
    tc.add_argument("--session-id")


def add_raw_parser(sub: Subparsers) -> None:
    # raw
    rw = sub.add_parser("raw", help="raw capture files").add_subparsers(dest="sub", required=True)
    rp = rw.add_parser(
        "preserve",
        help="copy transcripts this feature's raw captures reference into its .agent/raw/ transcripts/",
    )
    rp.add_argument("feature")
    rp.add_argument("--session-id", help="defaults to the current session")
    rw.add_parser(
        "redact",
        help="drop Claude Code's session-auth records from every preserved transcript",
    )


# ---------------------------------------------------------------- feature


def feature_new(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.feature_new(
            root,
            slug=args.slug,
            title=args.title,
            status=args.status,
            tickets=args.ticket,
            repos=args.repo,
            tags=args.tag,
            use=args.use,
            session_id=args.session_id,
        )
    )


def feature_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.feature_list(
            root, status=args.status, include_closed=args.all, include_requests=args.include_requests
        )
    )


def feature_show(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.feature_show(root, feature=args.feature, session_id=args.session_id))


def feature_use(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.feature_use(root, feature=args.feature, session_id=args.session_id, switch=args.switch)
    )


def feature_current(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.feature_current(root, session_id=args.session_id))


def feature_set(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.feature_set(
            root,
            feature=args.feature,
            session_id=args.session_id,
            status=args.status,
            title=args.title,
            add_ticket=args.add_ticket,
            remove_ticket=args.remove_ticket,
            add_repo=args.add_repo,
            remove_repo=args.remove_repo,
            add_adr=args.add_adr,
            remove_adr=args.remove_adr,
            add_tag=args.add_tag,
            remove_tag=args.remove_tag,
            reason=args.reason,
        )
    )


def raw_preserve(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.raw_preserve(root, feature=args.feature, session_id=args.session_id))


def raw_redact(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.raw_redact(root))


def feature_close(root: Path, args: argparse.Namespace, out: Output) -> int:
    # ops does the cleanup, so the MCP tool and the CLI behave the same (see ADR-0005). This only
    # reports what it did.
    op = ops.feature_close(
        root,
        feature=args.feature,
        status=args.status,
        note=args.note,
        session_id=args.session_id,
    )
    if op.code == EXIT_OK:
        for path in as_list(op.data.get("worktrees_removed")):
            sys.stderr.write(f"note: removed worktree: {path}\n")
        for path in as_list(op.data.get("worktrees_kept")):
            sys.stderr.write(f"note: worktree kept (dirty): {path}\n")
        if op.data.get("cleanup_error"):
            sys.stderr.write(f"note: worktree cleanup skipped: {op.data['cleanup_error']}\n")
    return out.done(op)


# ---------------------------------------------------------------- task


def task_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.task_list(root, feature=args.feature, open_only=args.open, session_id=args.session_id)
    )


def task_done(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.task_done(root, task=args.task, feature=args.feature, note=args.note, session_id=args.session_id)
    )


def task_undo(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.task_undo(root, task=args.task, feature=args.feature, session_id=args.session_id))


def task_add(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.task_add(root, text=args.text, feature=args.feature, session_id=args.session_id))


def task_supersede(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.task_supersede(
            root,
            task=args.task,
            reason=args.reason,
            link=args.link,
            feature=args.feature,
            session_id=args.session_id,
        )
    )


def task_check(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.task_check(root, feature=args.feature, session_id=args.session_id))


HANDLERS = {
    ("feature", "new"): feature_new,
    ("feature", "list"): feature_list,
    ("feature", "show"): feature_show,
    ("feature", "use"): feature_use,
    ("feature", "current"): feature_current,
    ("feature", "set"): feature_set,
    ("feature", "close"): feature_close,
    ("raw", "preserve"): raw_preserve,
    ("raw", "redact"): raw_redact,
    ("task", "list"): task_list,
    ("task", "done"): task_done,
    ("task", "undo"): task_undo,
    ("task", "add"): task_add,
    ("task", "supersede"): task_supersede,
    ("task", "check"): task_check,
}
