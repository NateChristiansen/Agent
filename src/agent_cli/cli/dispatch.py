"""`dispatch`: the headless-worker front end."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import dispatch as dispatch_mod
from .. import ops
from .. import repos as repos_mod
from ..core import EXIT_OK, Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # dispatch
    dp = sub.add_parser(
        "dispatch", help="send work to a headless worker inside a product repo"
    ).add_subparsers(dest="sub", required=True)
    dn = dp.add_parser(
        "new",
        help="create and start a dispatch",
        description="Non-exempt launches require runnable dispatch.verify commands and explicit "
        "verification timeouts. A preview describes planned checks, not verified work.",
    )
    dn.add_argument("repo", help="registry name or short")
    dn.add_argument("--feature", default="current")
    dn.add_argument(
        "--task", nargs="*", action="extend", default=[], help="task numbers handed to the worker"
    )
    src = dn.add_mutually_exclusive_group()
    src.add_argument("--prompt", help="the work order text")
    src.add_argument("--prompt-file", help="file holding the work order")
    src.add_argument("--stdin", action="store_true", help="read the work order from stdin")
    dn.add_argument("--budget", type=float)
    dn.add_argument("--reason", help="required when --budget exceeds the repo default")
    dn.add_argument(
        "--timeout",
        type=float,
        help="minutes per attempt for this dispatch; overrides the repo's dispatch.timeout_minutes",
    )
    dn.add_argument(
        "--row",
        help="a rubric row (probe, review, mechanical, build, design): derives the model and "
        "budget from harness/dispatch-rubric.md. Effort stays at the model's default. A repo's "
        "pinned dispatch.model and --model/--budget still win where given",
    )
    dn.add_argument(
        "--provider",
        choices=["claude", "codex"],
        default="claude",
        help="worker provider; a Codex worker's budget is a soft cap (no hard max_budget_usd)",
    )
    dn.add_argument("--model")
    dn.add_argument("--effort", choices=repos_mod.EFFORT_LEVELS)
    dn.add_argument(
        "--choice-reason", help="one line: why this model/effort/budget (harness/dispatch-rubric.md)"
    )
    dn.add_argument("--just-go", action="store_true", help="the owner waved the preview for this one")
    dn.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="launch even if preflight found a command with no matching permission rule (say why in the preview)",
    )
    dn.add_argument("--watch", action="store_true", help="run the supervisor inline instead of detaching")
    dn.add_argument("--preview-id", help="launch the exact prepared order returned by --dry-run")
    dn.add_argument(
        "--dry-run",
        action="store_true",
        help="write a linked work-order preview without allocating a worker slot",
    )
    dn.add_argument("--branch")
    dn.add_argument("--base")
    dn.add_argument("--target", help="artifact branch or commit; supplies the base for a review row")
    dn.add_argument(
        "--mcp",
        metavar="PROFILE",
        action="append",
        default=[],
        help="give the worker the MCP servers of this agent.yaml `worker_mcp:` profile (repeatable; "
        "`agent worker-mcp list` shows them)",
    )
    dn.add_argument("--session-id")
    dn.add_argument(
        "--writes",
        action="append",
        metavar="PATH",
        help="a path this dispatch may change (repeatable); overlapping running work refuses it",
    )
    dn.add_argument(
        "--read-only",
        action="store_true",
        help="a review that changes nothing durable (writes: []): blocks no other work, fails if it leaves changes",
    )
    dn.add_argument(
        "--no-checks",
        action="store_true",
        help="explicitly exempt this dispatch from report checks and supervisor verification; "
        "does not make worker permissions read-only",
    )
    dn.add_argument(
        "--escalate-from",
        metavar="ID",
        help="reuse that dispatch's repo/feature/branch/worktree, step up one model rung and "
        "keep its effort (including omission) unless --effort is passed, "
        "and prefix the work order with its last instruction",
    )
    dl = dp.add_parser("list", help="dispatches (non-terminal and recent by default)")
    dl.add_argument("--feature")
    dl.add_argument("--all", action="store_true")
    ds = dp.add_parser("show", help="one dispatch; clears its chat-pending flag")
    ds.add_argument("id")
    dsp = dp.add_parser("spans", help="one dispatch's lifecycle phases, waits and per-attempt cost")
    dsp.add_argument("id")
    dw = dp.add_parser("watch", help="print the worker's transcript as text")
    dw.add_argument("id")
    dw.add_argument("--follow", action="store_true")
    dw.add_argument(
        "--digest",
        action="store_true",
        help="one short line per event (said / ran / failed) instead of the full text; "
        "cheap enough to follow from a background task",
    )
    dr = dp.add_parser(
        "resume",
        help="answer a worker and continue its session",
        description="Rechecks current runnable verification configuration before resuming. "
        "Only the dispatch's original explicit no-check exemption skips verification.",
    )
    dr.add_argument("id")
    ans = dr.add_mutually_exclusive_group(required=True)
    ans.add_argument("--answer")
    ans.add_argument("--stdin", action="store_true")
    dr.add_argument("--budget", type=float)
    dr.add_argument("--reason")
    dr.add_argument(
        "--timeout",
        type=float,
        help="minutes per attempt from now on; a resume otherwise keeps the clock the dispatch launched with",
    )
    dr.add_argument("--watch", action="store_true")
    dr.add_argument(
        "--writes",
        action="append",
        metavar="PATH",
        help="narrow the dispatch's declared paths for this attempt (repeatable); never widens them",
    )
    dr.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="resume even if the answer names a command no permission rule allows (say why)",
    )
    dr.add_argument(
        "--again",
        action="store_true",
        help="resume past the 3-resume escalation guard; the guard suggests --escalate-from instead",
    )
    dsay = dp.add_parser("say", help="send a message to a RUNNING worker, mid-turn")
    dsay.add_argument("id")
    dsay.add_argument("message", nargs="?", help="omit with --stdin")
    dsay.add_argument("--stdin", action="store_true", help="read the message from stdin")
    dc = dp.add_parser("cancel", help="stop a running worker; worktree kept")
    dc.add_argument("id")
    dab = dp.add_parser("abandon", help="close a finished dispatch nobody will resume; worktree kept")
    dab.add_argument("id")
    dab.add_argument("--note", required=True, help="one line: why nobody will resume it")
    dop = dp.add_parser(
        "open",
        help="open a worker in an Obsidian terminal: a watch while it runs, claude --resume once stopped",
    )
    dop.add_argument("id")
    dop.add_argument("--print", action="store_true", help="print the command instead of opening a terminal")
    dak = dp.add_parser("ack", help="mark a finished dispatch dealt with; it leaves the Workers view")
    dak.add_argument("id")
    dak.add_argument("--note", help="optional: what was done (pushed, read, nothing to do)")
    dco = dp.add_parser("collect", help="ingest finished workers into handoffs and the log")
    dco.add_argument("id", nargs="?")
    dco.add_argument("--wait", type=float, default=0)
    dcf = dp.add_parser(
        "collect-files", help="copy selected untracked worker files into private handoff artifacts"
    )
    dcf.add_argument("id")
    dcf.add_argument(
        "--path",
        action="append",
        required=True,
        dest="paths",
        help="exact worktree-relative file (repeatable)",
    )
    drv = dp.add_parser(
        "reverify", help="rerun only the supervisor checks for a terminal dispatch's recorded head"
    )
    drv.add_argument("id")
    dcl = dp.add_parser("cleanup", help="remove a feature's worktrees or one dispatch's tree (branches kept)")
    scope = dcl.add_mutually_exclusive_group(required=True)
    scope.add_argument("feature", nargs="?", help="feature reference")
    scope.add_argument("--dispatch-id", help="remove only this dispatch's tree")
    dcl.add_argument("--force", action="store_true", help="remove dirty worktrees too")
    dcl.add_argument("--dry-run", action="store_true", help="list what would be removed; remove nothing")
    dcl.add_argument(
        "--delete-branches",
        action="store_true",
        help="also delete removed trees' resolved, unneeded branches",
    )
    drc = dp.add_parser("reconcile", help="drop a missing worktree's Git registration (preview by default)")
    drc.add_argument("id")
    drc.add_argument("--apply", action="store_true")
    drs = dp.add_parser(
        "restore", help="put an unfinished dispatch's lost checkout back (preview by default)"
    )
    drs.add_argument("id")
    drs.add_argument("--head", help="the commit the checkout was at; default: the recorded one")
    drs.add_argument("--apply", action="store_true")
    ddf = dp.add_parser(
        "discard-files", help="delete an abandoned worker's named scratch files (preview by default)"
    )
    ddf.add_argument("id")
    ddf.add_argument("--path", action="append", required=True, dest="paths", help="exact file (repeatable)")
    ddf.add_argument(
        "--sha256",
        action="append",
        default=[],
        metavar="PATH=SHA256",
        help="the preview's digest for each path; required with --apply",
    )
    ddf.add_argument("--apply", action="store_true", help="delete; only after the owner agrees")
    dru = dp.add_parser("run", help=argparse.SUPPRESS)
    dru.add_argument("id")
    dle = dp.add_parser(
        "ledger", help="deterministic outcome ledger: finished dispatches by PR, feature, or week"
    )
    dle.add_argument("--by", choices=list(dispatch_mod.LEDGER_BY), default="pr")
    dle.add_argument(
        "--refresh", action="store_true", help="force-refresh PR data from gh first (real API calls)"
    )
    dle.add_argument("--repo", help="scope --refresh's gh calls to one repo (registry name or short)")


def dispatch_new(root: Path, args: argparse.Namespace, out: Output) -> int:
    if args.stdin:
        prompt = sys.stdin.read()
    elif args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    else:
        prompt = args.prompt or ""
    # `--row` and `--provider` resolve in `dispatch/lifecycle.py`, not here, so
    # the MCP tool gets the same rubric as the CLI.
    op = ops.dispatch_new(
        root,
        repo=args.repo,
        feature=args.feature,
        prompt=prompt,
        tasks=args.task,
        budget_usd=args.budget,
        budget_reason=args.reason,
        timeout_minutes=args.timeout,
        model=args.model,
        effort=args.effort,
        choice_reason=args.choice_reason,
        provider=args.provider,
        row=args.row,
        just_go=args.just_go,
        branch=args.branch,
        base=args.base,
        target=args.target,
        dry_run=args.dry_run,
        preview_id=args.preview_id,
        watch=args.watch,
        session_id=args.session_id,
        allow_unmatched=args.allow_unmatched,
        mcp=args.mcp,
        no_checks=args.no_checks,
        escalate_from=args.escalate_from,
        writes=[] if args.read_only else args.writes,
    )
    # the dry-run preview is `op.text` now (ops.dispatch_preview_text), so the MCP tool renders
    # the same table this prints instead of handing back a bare dict
    return out.done(op)


def dispatch_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_list(root, feature=args.feature, include_all=args.all))


def dispatch_show(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_show(root, dispatch_id=args.id))


def dispatch_spans(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_spans(root, dispatch_id=args.id))


def dispatch_watch(root: Path, args: argparse.Namespace, out: Output) -> int:
    for line in dispatch_mod.watch(root, dispatch_id=args.id, follow=args.follow, digest=args.digest):
        sys.stdout.write(line if line.endswith("\n") else line + "\n")
        # Flush per line or a follow-tail shows nothing. Python block-buffers stdout whenever
        # it is not a tty, so piping this into a file or a background task swallowed every line
        # until 4-8KB accumulated. The first live tail of a worker sat empty for ten minutes
        # with a 1.5MB transcript waiting to replay.
        sys.stdout.flush()
    return EXIT_OK


def dispatch_resume(root: Path, args: argparse.Namespace, out: Output) -> int:
    answer = sys.stdin.read() if args.stdin else args.answer
    return out.done(
        ops.dispatch_resume(
            root,
            dispatch_id=args.id,
            answer=answer,
            answer_by="owner",
            budget_usd=args.budget,
            budget_reason=args.reason,
            timeout_minutes=args.timeout,
            watch=args.watch,
            allow_unmatched=args.allow_unmatched,
            again=args.again,
            writes=args.writes,
        )
    )


def dispatch_say(root: Path, args: argparse.Namespace, out: Output) -> int:
    message = sys.stdin.read() if args.stdin else (args.message or "")
    return out.done(ops.dispatch_say(root, dispatch_id=args.id, message=message))


def dispatch_cancel(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_cancel(root, dispatch_id=args.id))


def dispatch_abandon(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_abandon(root, dispatch_id=args.id, note=args.note))


def dispatch_open(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_open(root, dispatch_id=args.id, print_only=args.print))


def dispatch_ack(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_ack(root, dispatch_id=args.id, note=args.note))


def dispatch_collect(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_collect(root, dispatch_id=args.id, wait=args.wait))


def dispatch_cleanup(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.dispatch_cleanup(
            root,
            feature=args.feature,
            dispatch_id=args.dispatch_id,
            force=args.force,
            dry_run=args.dry_run,
            delete_branches=args.delete_branches,
        )
    )


def dispatch_reconcile(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_reconcile(root, dispatch_id=args.id, apply=args.apply))


def dispatch_restore(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_restore(root, dispatch_id=args.id, head=args.head, apply=args.apply))


def dispatch_discard_files(root: Path, args: argparse.Namespace, out: Output) -> int:
    hashes = dict(item.split("=", 1) for item in args.sha256 if "=" in item)
    return out.done(
        ops.dispatch_discard_files(
            root, dispatch_id=args.id, paths=args.paths, plan_hashes=hashes or None, apply=args.apply
        )
    )


def dispatch_collect_files(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_collect_files(root, dispatch_id=args.id, paths=args.paths))


def dispatch_reverify(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_reverify(root, dispatch_id=args.id))


def dispatch_run(root: Path, args: argparse.Namespace, out: Output) -> int:
    d = dispatch_mod.run(root, dispatch_id=args.id)
    out.emit(d, ops.dispatch_line(d))
    return EXIT_OK


def dispatch_ledger(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.dispatch_ledger(root, by=args.by, refresh=args.refresh, repo=args.repo))


HANDLERS = {
    ("dispatch", "new"): dispatch_new,
    ("dispatch", "list"): dispatch_list,
    ("dispatch", "show"): dispatch_show,
    ("dispatch", "spans"): dispatch_spans,
    ("dispatch", "watch"): dispatch_watch,
    ("dispatch", "resume"): dispatch_resume,
    ("dispatch", "say"): dispatch_say,
    ("dispatch", "cancel"): dispatch_cancel,
    ("dispatch", "abandon"): dispatch_abandon,
    ("dispatch", "ack"): dispatch_ack,
    ("dispatch", "open"): dispatch_open,
    ("dispatch", "collect"): dispatch_collect,
    ("dispatch", "cleanup"): dispatch_cleanup,
    ("dispatch", "collect-files"): dispatch_collect_files,
    ("dispatch", "reconcile"): dispatch_reconcile,
    ("dispatch", "restore"): dispatch_restore,
    ("dispatch", "discard-files"): dispatch_discard_files,
    ("dispatch", "reverify"): dispatch_reverify,
    ("dispatch", "run"): dispatch_run,
    ("dispatch", "ledger"): dispatch_ledger,
}
