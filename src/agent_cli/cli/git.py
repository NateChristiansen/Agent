"""`git` and `export`: this repo's commit and push boundaries (see ADR-0026)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import export, gitgate
from ..core import EXIT_NOT_FOUND, EXIT_OK, Output
from ..ops._shared import Op
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    gt = sub.add_parser(
        "git", help="the git hooks that gate commits and pushes in this repo (ADR-0026)"
    ).add_subparsers(dest="sub", required=True)
    gt.add_parser("pre-commit", help="check the staged Python and instruction files; run by the hook")
    pp = gt.add_parser("pre-push", help="refuse a push from this checkout; run by the hook")
    pp.add_argument("remote", nargs="?", help="passed by git; unused")
    pp.add_argument("url", nargs="?", help="passed by git; unused")
    gh = gt.add_parser("hooks", help="show the hooks' state; --install writes them")
    gh.add_argument("--install", action="store_true", help="write missing or stale hooks")
    ex = sub.add_parser(
        "export", help="build and verify the squashed public export, then stop before pushing"
    )
    ex.add_argument("dest", help="an empty directory outside this checkout")
    ex.add_argument("-m", "--message", required=True, help="the export commit's message")
    ex.add_argument("--remote", required=True, help="URL of the new destination repository")


def _report(out: Output, problems: list[str], clean: str) -> int:
    if problems and not out.as_json:
        print("\n".join(problems), file=sys.stderr)
        return EXIT_NOT_FOUND
    return out.done(Op({"problems": problems}, clean, code=EXIT_NOT_FOUND if problems else EXIT_OK))


def git_pre_commit(root: Path, args: argparse.Namespace, out: Output) -> int:
    return _report(out, gitgate.pre_commit(root), "")


def git_pre_push(root: Path, args: argparse.Namespace, out: Output) -> int:
    """The one pre-push hook, over one strict reading of git's stdin: the export-only gate, then
    the pass record each pushed tip needs (see ADR-0041)."""
    from .. import push_input, push_records

    updates = push_input.parse_updates(sys.stdin.read(push_input.MAX_INPUT_CHARS + 1))
    problems = gitgate.pre_push(root)
    if problems:
        return _report(out, problems, "")
    unrecorded = push_records.problems(root, updates)
    if unrecorded:
        head = "push refused: every pushed tip needs a full pass record (ADR-0041)"
        return _report(out, [head, *unrecorded], "")
    return _report(out, [], "")


def git_hooks(root: Path, args: argparse.Namespace, out: Output) -> int:
    data = gitgate.install(root) if args.install else gitgate.inspect(root)
    return out.done(Op(data, gitgate.describe(data)))


def export_build(root: Path, args: argparse.Namespace, out: Output) -> int:
    data = export.build(root, Path(args.dest), message=args.message, remote=args.remote)
    return out.done(Op(data, export.format_result(data), code=EXIT_OK if data["ok"] else EXIT_NOT_FOUND))


HANDLERS = {
    ("git", "pre-commit"): git_pre_commit,
    ("git", "pre-push"): git_pre_push,
    ("git", "hooks"): git_hooks,
    ("export", None): export_build,
}
