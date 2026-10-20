"""MCP servers this vault runs: `mcp-servers` for the manager's own, installed from a pinned
manifest, and `worker-mcp` for the extra ones a dispatch can hand its worker (agent.yaml
`worker_mcp:`).

Both live here because they are the same subject from two ends. They are separate top-level
commands because `agent mcp` already means "serve the CLI as MCP tools over stdio" and takes
flags, not a subcommand.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import worker_mcp as worker_mcp_mod
from ..core import EXIT_OK, Output
from ..ops import Op
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    ms = sub.add_parser(
        "mcp-servers", help="the manager's own MCP servers, installed from a pinned manifest"
    ).add_subparsers(dest="sub", required=True)
    mi = ms.add_parser(
        "install",
        help="npm ci the declared servers, then check each installed binary actually starts",
    )
    mi.add_argument(
        "--update",
        action="store_true",
        help="npm install instead, rewriting the lock: needed after you add or change a server",
    )

    wm = sub.add_parser(
        "worker-mcp", help="extra MCP servers for a dispatch (agent.yaml `worker_mcp:`, `dispatch new --mcp`)"
    ).add_subparsers(dest="sub", required=True)
    wm.add_parser("list", help="configured profiles, and whether each one's required file is present")
    we = wm.add_parser(
        "exec",
        help="exec a profile's server from its output directory (a worker's mcp config runs this)",
    )
    we.add_argument("profile")
    we.add_argument("--output-dir", required=True, help="where the server runs and writes its files")
    we.add_argument("--dispatch", default="", help="the dispatch id, for `{dispatch_id}` in the profile")


def worker_mcp_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    rows = worker_mcp_mod.listing(root)
    return out.done(Op(rows, worker_mcp_mod.format_listing(rows)))


def worker_mcp_exec(root: Path, args: argparse.Namespace, out: Output) -> int:
    worker_mcp_mod.exec_profile(root, args.profile, Path(args.output_dir), args.dispatch)
    return EXIT_OK  # unreachable: exec_profile execs


def mcp_servers_install(root: Path, args: argparse.Namespace, out: Output) -> int:
    from .. import ops as ops_mod

    return out.done(ops_mod.mcp_servers_install(root, update=args.update))


HANDLERS = {
    ("mcp-servers", "install"): mcp_servers_install,
    ("worker-mcp", "list"): worker_mcp_list,
    ("worker-mcp", "exec"): worker_mcp_exec,
}
