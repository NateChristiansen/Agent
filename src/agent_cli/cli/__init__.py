"""argparse tree for `agent`. Every subcommand maps to one function in a sibling module.

The module is a package, split the way `dispatch/` was: one module per domain. `feature`
(incl. task and raw), `log`, `adr`, `status`, `journal`, `repos`, `session`, `lease`,
`scratch`, `dispatch`, `notes`, `worker-mcp`, and `infra` for capture, obsidian,
hook, mcp and doctor. Each exposes `add_parser(sub)`, which hangs
its subparsers off the top-level `subparsers` object, and `HANDLERS`, its `(cmd, sub) ->
handler` entries. This file assembles `build_parser`, `HANDLERS` and `main`. It re-exports
every handler so `cli.<name>` keeps resolving as it did when this was one file.

`build_parser` calls the `add_parser` functions in the order the old single-file cli
registered them. argparse lists subcommands in registration order, and the help text has
to stay byte-identical. That is why `feature.add_raw_parser` and `infra.add_tail_parser`
exist: `raw` sat between `scratch` and `capture`, and `hook`/`mcp`/`doctor` sat after
`dispatch`.

A test that MONKEYPATCHES a handler must patch the submodule that owns it
(`cli.feature.feature_new`, not `cli.feature_new`). A re-export here is a separate binding
the router never reads. `HANDLERS` itself is the one dict the router does read, so
`monkeypatch.setitem(cli.HANDLERS, ...)` still works.
"""

from __future__ import annotations

import argparse
import copy
import functools
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .. import __version__
from ..core import EXIT_OK, AgentError, Output, find_root
from ..entry import utf8_streams as _utf8_streams
from . import adr as adr_cli
from . import dispatch as dispatch_cli
from . import feature as feature_cli
from . import git as git_cli
from . import infra as infra_cli
from . import journal as journal_cli
from . import land as land_cli
from . import lease as lease_cli
from . import lesson as lesson_cli
from . import log as log_cli
from . import notes as notes_cli
from . import notes_sync as notes_sync_cli
from . import peer as peer_cli
from . import peer_net as peer_net_cli
from . import procs as procs_cli
from . import repos as repos_cli
from . import request as request_cli
from . import scratch as scratch_cli
from . import session as session_cli
from . import status as status_cli
from . import telemetry as telemetry_cli
from . import tidy as tidy_cli
from . import verify as verify_cli
from . import worker_mcp as worker_mcp_cli
from . import worktree as worktree_cli

# Re-exports: the names `cli.<handler>` resolved to before the split.
from .adr import adr_new
from .dispatch import (
    dispatch_abandon,
    dispatch_ack,
    dispatch_cancel,
    dispatch_cleanup,
    dispatch_collect,
    dispatch_ledger,
    dispatch_list,
    dispatch_new,
    dispatch_open,
    dispatch_resume,
    dispatch_run,
    dispatch_say,
    dispatch_show,
    dispatch_watch,
)
from .feature import (
    feature_close,
    feature_current,
    feature_list,
    feature_new,
    feature_set,
    feature_show,
    feature_use,
    raw_preserve,
    task_add,
    task_done,
    task_list,
    task_undo,
)
from .infra import (
    capture_cmd,
    doctor_cmd,
    hook_cmd,
    mcp_cmd,
    obsidian_daily,
    obsidian_open,
    obsidian_search,
    obsidian_setup,
    setup_cmd,
)
from .journal import journal_collect, journal_missing, journal_write
from .lease import lease_list, lease_release, lease_take, lease_wait
from .log import log_cmd
from .notes import notes_init, notes_link, notes_root, notes_status
from .repos import repos_list, repos_status, repos_sync
from .scratch import scratch_path
from .session import session_get, session_list, session_prune, session_set
from .status import status_check, status_line, status_render
from .worker_mcp import worker_mcp_exec, worker_mcp_list
from .worktree import worktree_link, worktree_list, worktree_new, worktree_remove

__all__ = [
    "HANDLERS",
    "Handler",
    "build_parser",
    "parse",
    "dispatch",
    "main",
    # adr
    "adr_new",
    # dispatch
    "dispatch_abandon",
    "dispatch_ack",
    "dispatch_cancel",
    "dispatch_cleanup",
    "dispatch_collect",
    "dispatch_ledger",
    "dispatch_list",
    "dispatch_new",
    "dispatch_open",
    "dispatch_resume",
    "dispatch_run",
    "dispatch_say",
    "dispatch_show",
    "dispatch_watch",
    # feature
    "feature_close",
    "feature_current",
    "feature_list",
    "feature_new",
    "feature_set",
    "feature_show",
    "feature_use",
    "raw_preserve",
    "task_add",
    "task_done",
    "task_list",
    "task_undo",
    # infra
    "capture_cmd",
    "doctor_cmd",
    "hook_cmd",
    "mcp_cmd",
    "obsidian_daily",
    "obsidian_open",
    "obsidian_search",
    "obsidian_setup",
    "setup_cmd",
    # journal
    "journal_collect",
    "journal_missing",
    "journal_write",
    # lease
    "lease_list",
    "lease_release",
    "lease_take",
    "lease_wait",
    # log
    "log_cmd",
    # notes
    "notes_init",
    "notes_link",
    "notes_root",
    "notes_status",
    # repos
    "repos_list",
    "repos_status",
    "repos_sync",
    # scratch
    "scratch_path",
    # session
    "session_get",
    "session_list",
    "session_prune",
    "session_set",
    # status
    "status_check",
    "status_line",
    "status_render",
    # worker_mcp
    "worker_mcp_exec",
    "worker_mcp_list",
    # worktree
    "worktree_link",
    "worktree_list",
    "worktree_new",
    "worktree_remove",
]

Handler = Callable[[Path, argparse.Namespace, Output], int]

_MODULES = (
    feature_cli,
    log_cli,
    lesson_cli,
    adr_cli,
    status_cli,
    journal_cli,
    repos_cli,
    session_cli,
    lease_cli,
    worktree_cli,
    peer_cli,
    scratch_cli,
    dispatch_cli,
    infra_cli,
    notes_cli,
    worker_mcp_cli,
    procs_cli,
    git_cli,
    notes_sync_cli,
    peer_net_cli,
    verify_cli,
    land_cli,
    tidy_cli,
    telemetry_cli,
    request_cli,
)

HANDLERS: dict[tuple[str, str | None], Handler] = {}
for _module in _MODULES:
    for _key, _handler in _module.HANDLERS.items():
        HANDLERS[_key] = _handler


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent", description="The manager-of-agents CLI.")
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.add_argument("--root", help="harness root (default: $AGENT_ROOT or walk up to bin/agent)")
    p.add_argument("--version", action="version", version=f"agent {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    feature_cli.add_parser(sub)  # feature, task
    log_cli.add_parser(sub)
    lesson_cli.add_parser(sub)
    adr_cli.add_parser(sub)
    status_cli.add_parser(sub)
    journal_cli.add_parser(sub)
    repos_cli.add_parser(sub)
    session_cli.add_parser(sub)
    lease_cli.add_parser(sub)
    worktree_cli.add_parser(sub)
    peer_cli.add_parser(sub)
    scratch_cli.add_parser(sub)
    feature_cli.add_raw_parser(sub)  # raw
    infra_cli.add_parser(sub)  # capture, obsidian
    dispatch_cli.add_parser(sub)
    infra_cli.add_tail_parser(sub)  # hook, mcp, doctor, setup
    notes_cli.add_parser(sub)  # the help order above is the old single-file cli's
    worker_mcp_cli.add_parser(sub)
    procs_cli.add_parser(sub)
    git_cli.add_parser(sub)
    notes_sync_cli.add_parser(sub)
    peer_net_cli.add_parser(sub)
    verify_cli.add_parser(sub)
    land_cli.add_parser(sub)
    tidy_cli.add_parser(sub)
    telemetry_cli.add_parser(sub)
    request_cli.add_parser(sub)
    return p


@functools.cache
def _parser() -> tuple[argparse.ArgumentParser, tuple[tuple[str, Any], ...]]:
    """The parser, built once per process (68 ms each time), with every action's mutable default.

    argparse hands an omitted list-valued option its default object itself, so a reused parser
    would share that list between calls; `parse` copies it."""
    parser = build_parser()
    mutable: list[tuple[str, Any]] = []
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:  # noqa: SLF001 — argparse exposes no public action list
            default: object = action.default
            if isinstance(default, (list, dict, set)):
                mutable.append((action.dest, cast(object, default)))
            choices: object = action.choices
            if isinstance(choices, dict):  # a subparsers action: its parsers, by command name
                pending += [
                    v
                    for v in cast("dict[str, object]", choices).values()
                    if isinstance(v, argparse.ArgumentParser)
                ]
    return parser, tuple(mutable)


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Parse with the shared parser; no namespace ever holds a default object another holds."""
    parser, mutable = _parser()
    args = parser.parse_args(argv)
    for dest, default in mutable:
        if getattr(args, dest, None) is default:
            setattr(args, dest, copy.copy(default))
    return args


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    args = parse(argv)
    out = Output(as_json=args.json)
    try:
        root = find_root(args.root)
        return dispatch(root, args, out)
    except AgentError as exc:
        if args.cmd == "hook" and not args.probe:  # ordinary hooks fail open; doctor probes do not
            return EXIT_OK
        sys.stderr.write(f"error: {exc.message}\n")
        if args.json:
            import json

            sys.stdout.write(json.dumps({"error": exc.message, "code": exc.code}) + "\n")
        return exc.code
    except Exception as exc:  # noqa: BLE001 — an LLM caller must never see a traceback
        if args.cmd == "hook" and not args.probe:
            return EXIT_OK
        message = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(f"error: {message}\n")
        if args.json:
            import json

            sys.stdout.write(json.dumps({"error": message, "code": 1}) + "\n")
        return 1


def dispatch(root: Path, args: argparse.Namespace, out: Output) -> int:
    handler = HANDLERS[(args.cmd, getattr(args, "sub", None))]
    return handler(root, args, out)


if __name__ == "__main__":
    sys.exit(main())
