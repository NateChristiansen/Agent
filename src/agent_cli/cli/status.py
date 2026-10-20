"""`status`: the generated files, plus the statusLine command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import ops
from ..core import EXIT_OK, Output
from ..records.narrow import as_dict, as_float
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # status
    s = sub.add_parser("status", help="generated vault views / ADR indexes / status.json").add_subparsers(
        dest="sub", required=True
    )
    s.add_parser("render", help="regenerate the generated files")
    s.add_parser("check", help="exit 1 if the generated files are stale")
    s.add_parser(
        "line",
        help=(
            "one line for Claude Code's statusLine: this session's name and cost, running and "
            "queued dispatches, what is owed to you, this session's leases, other live managers"
        ),
    )


def status_render(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.status_render(root))


def status_check(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.status_check(root))


def status_line(root: Path, args: argparse.Namespace, out: Output) -> int:
    """Claude Code's `statusLine` command. Must
    never fail visibly and must never block. It reads the statusLine JSON from stdin only
    when stdin is not a TTY, so a human running this by hand does not hang waiting for input.
    It swallows any error from a malformed stdin payload or from `ops.status_line` itself.
    Empty output beats a crash or a traceback in the status bar."""
    session_id = None
    cost_usd = None
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                import json

                # Field names, from https://code.claude.com/docs/en/statusline: top-level
                # `session_id` (unique per session) and `cwd` (== `workspace.current_dir`,
                # the live working directory). Claude Code re-runs this command on session
                # start/resume, on every new assistant message, on `/compact` finishing, on a
                # permission-mode or vim-mode change, on a `statusLine.command` change, and on
                # a `refreshInterval` timer if one is set. Debounced 300ms, so a burst of
                # triggers collapses into one run. `cwd` is read here for completeness with
                # the field list above. Nothing in this command's output depends on it today.
                payload = json.loads(raw)
                session_id = payload.get("session_id")
                # `cost.total_cost_usd`: what this session has cost so far, priced by Claude
                # Code itself. The only place the harness is handed a dollar figure for its own
                # work; everything else it can see is tokens.
                cost = as_dict(payload.get("cost"))
                total = cost.get("total_cost_usd")
                if isinstance(total, (int, float)) and not isinstance(total, bool):
                    cost_usd = as_float(total)
    except Exception:  # noqa: BLE001 — malformed stdin must not break the status line
        session_id = None
        cost_usd = None
    try:
        op = ops.status_line(root, session_id=session_id, cost_usd=cost_usd)
    except Exception:  # noqa: BLE001 — never fail visibly; empty output beats a crash
        return EXIT_OK
    return out.done(op)


HANDLERS = {
    ("status", "render"): status_render,
    ("status", "check"): status_check,
    ("status", "line"): status_line,
}
