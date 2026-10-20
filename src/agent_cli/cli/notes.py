"""Private notes setup, inspection and scoped feature-document editing (see ADR-0012).

Thin wrappers over `ops.notes_*`. No MCP tool: linking a second repo into the vault is a
setup act at a terminal, not something a session does mid-task.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ops
from ..core import Output
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    n = sub.add_parser(
        "notes", help="the private notes repo: the paths this harness does not track"
    ).add_subparsers(dest="sub", required=True)

    ni = n.add_parser("init", help="build a fresh notes repo skeleton, ready for `setup --notes`")
    ni.add_argument("path", help="where the notes repo goes, e.g. ../Agent-notes")

    nl = n.add_parser("link", help="symlink the private set in from an existing notes repo")
    nl.add_argument("path", help="the notes repo to link from")

    n.add_parser("status", help="what every private path is here: linked, plain, missing, broken")

    n.add_parser("root", help="the git repo a private path belongs to, for `git -C <root> add`")

    nd = n.add_parser("document", help="preview a feature document replacement; apply explicitly")
    nd.add_argument("target", help="features/<existing NNN-slug>/<name>.md")
    nd.add_argument("--from-file", required=True, help="UTF-8 file containing the full replacement")
    nd.add_argument("--apply", action="store_true", help="write the replacement (default: unified diff only)")
    nd.add_argument(
        "--expect-sha256", help="whole-file digest from preview; required for existing-file apply"
    )
    nd.add_argument("--create", action="store_true", help="create a new top-level feature document")
    nd.add_argument("--expect-absent", action="store_true", help="assert a new document does not exist")

    nw = n.add_parser("write", help="change a linked private file (agent.yaml) without severing its link")
    nw.add_argument("target", help="agent.yaml, .mcp.json or harness/voice.md")
    nw.add_argument("--from-file", help="UTF-8 file with the full new text (default: copy to a draft)")
    nw.add_argument("--apply", action="store_true", help="write it (default: unified diff only)")
    nw.add_argument("--expect-sha256", help="whole-file digest from the preview; required with --apply")


def notes_init(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.notes_init(root, path=args.path))


def notes_link(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.notes_link(root, path=args.path))


def notes_status(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.notes_status(root))


def notes_root(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.notes_root(root))


def notes_document(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.notes_document(
            root,
            target=args.target,
            from_file=args.from_file,
            apply=args.apply,
            expect_sha256=args.expect_sha256,
            create=args.create,
            expect_absent=args.expect_absent,
        )
    )


def notes_write(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        ops.notes_write(
            root,
            target=args.target,
            from_file=args.from_file,
            apply=args.apply,
            expect_sha256=args.expect_sha256,
        )
    )


HANDLERS = {
    ("notes", "init"): notes_init,
    ("notes", "link"): notes_link,
    ("notes", "status"): notes_status,
    ("notes", "root"): notes_root,
    ("notes", "document"): notes_document,
    ("notes", "write"): notes_write,
}
