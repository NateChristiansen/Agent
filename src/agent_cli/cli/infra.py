"""Everything that is not a domain of its own: `capture`, `obsidian`, `hook`, `mcp`, `doctor`.

Two registration functions, not one. The old single-file cli registered `capture` and
`obsidian` before `dispatch`, and the rest after it. `agent --help` lists
subcommands in registration order, so the split keeps that order byte-identical.
"""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from .. import capture as capture_mod
from .. import hooks as hooks_mod
from .. import ops
from ..core import EXIT_OK, Output, agent_root
from ..hooks import groups as hook_groups
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    # capture
    c = sub.add_parser("capture", help="write a hook payload to raw jsonl")
    c.add_argument("--kind", required=True, choices=capture_mod.KINDS)
    c.add_argument("--stdin", action="store_true", help="read the hook JSON from stdin")
    c.add_argument("--session-id")

    # obsidian
    ob = sub.add_parser("obsidian", help="Obsidian vault config and CLI wrapper").add_subparsers(
        dest="sub", required=True
    )
    ob.add_parser("setup", help="write/merge the curated .obsidian config")
    oo = ob.add_parser("open", help="open a vault note in Obsidian (no-op if the CLI is absent)")
    oo.add_argument("path", help="vault-relative path, e.g. features/001-x/plan.md")
    ob.add_parser("daily", help="open today's daily note")
    osr = ob.add_parser("search", help="search the vault through Obsidian")
    osr.add_argument("query")


def add_tail_parser(sub: Subparsers) -> None:
    # hook
    h = sub.add_parser("hook", help="Claude Code hook entry points (read hook JSON on stdin)")
    # A removed name stays a choice for one release, so an old session's wiring runs a no-op.
    h.add_argument("name", choices=(*hooks_mod.NAMES, *hook_groups.NAMES, *hooks_mod.ALIASES))
    h.add_argument(
        "--probe", action="store_true", help="run no-chaining strictly in an isolated temporary vault"
    )

    # mcp
    m = sub.add_parser("mcp", help="serve the CLI as MCP tools over stdio (Claude Code and Codex)")
    m.add_argument(
        "--profile",
        default="manager",
        choices=("manager", "worker"),
        help="manager: the full surface; worker: log only",
    )
    m.add_argument("--tools", help="comma-separated tool names to register (default: the profile's full set)")

    # doctor
    sub.add_parser("doctor", help="validate everything")

    # setup
    st = sub.add_parser(
        "setup", help="check the requirements, then configure the harness; re-runnable, any OS"
    )
    st.add_argument("--check", action="store_true", help="run the requirement checks only; configure nothing")
    st.add_argument(
        "--yes",
        action="store_true",
        help="install every missing requirement setup can install, without asking",
    )
    st.add_argument(
        "--notes",
        help="the private notes repo to symlink the untracked paths in from (ADR-0012); "
        "remembered in .agent/notes-root and reused when the flag is omitted",
    )
    st.add_argument(
        "--codex-sandbox",
        choices=("mxc", "elevated"),
        help="Windows sandbox trial or rollback; remembered in the project Codex profile",
    )
    st.add_argument(
        "--codex-inline",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Windows: persist --no-alt-screen after a successful scroll experiment (default: off)",
    )


# ---------------------------------------------------------------- capture / doctor


def capture_cmd(root: Path, args: argparse.Namespace, out: Output) -> int:
    # reading the hook JSON off stdin is the front end's job. Nothing in ops touches a stream.
    return out.done(
        ops.capture(
            root,
            kind=args.kind,
            payload=sys.stdin.read() if args.stdin else "{}",
            session_id=args.session_id,
        )
    )


def obsidian_setup(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.obsidian_setup(root))


def obsidian_open(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.obsidian_open(root, path=args.path))


def obsidian_daily(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.obsidian_daily(root))


def obsidian_search(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.obsidian_search(root, query=args.query))


def hook_cmd(root: Path, args: argparse.Namespace, out: Output) -> int:
    if args.probe:
        import json
        import tempfile

        # Only this pure guard is a runtime probe. Lifecycle hooks can read client homes or
        # invoke integrations, so doctor checks their command suffixes against the registry.
        if args.name != "no-chaining":
            raise ValueError("only no-chaining supports --probe; other handlers are checked statically")
        with tempfile.TemporaryDirectory(prefix="agent-hook-probe-") as directory:
            probe_root = Path(directory)
            agent_root(probe_root).mkdir()
            hooks_mod.HANDLERS[args.name](probe_root, {})
        sys.stdout.write(json.dumps({"probe": args.name, "ok": True}) + "\n")
        return EXIT_OK
    text, code = hooks_mod.run(root, args.name, sys.stdin.read())
    if text:
        sys.stdout.write(text + "\n")
    return code


def mcp_cmd(root: Path, args: argparse.Namespace, out: Output) -> int:
    from ..mcp import server as mcp_server  # lazy: hooks and plain CLI calls never pay the mcp import

    tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None
    mcp_server.serve(root, args.profile, tools)
    return EXIT_OK


def doctor_cmd(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(ops.doctor(root))


def setup_cmd(root: Path, args: argparse.Namespace, out: Output) -> int:
    # The one yes is asked here, not in ops: nothing in ops touches a stream.
    # `--yes` answers it; without a terminal to ask on, nothing installs and the guide prints.
    install = bool(args.yes)
    if not install and not args.check and sys.stdin.isatty():
        from .. import installs
        from .. import setup as setup_mod

        system = platform.system()
        todo = installs.installable(setup_mod.checks(system), system)
        if todo:
            names = ", ".join(t.name for t in todo)
            answer = input(f"Missing: {names}. Install them now? [Y/n] ").strip().lower()
            install = answer in ("", "y", "yes")
    notes = args.notes
    owner = None
    machine = None
    if not args.check and sys.stdin.isatty():
        if notes is None:
            notes = ask_notes_path(root)
        owner = ask_owner_name(root, notes)
        if not args.yes:
            machine = ask_machine_label(root)
    return out.done(
        ops.setup(
            root,
            check_only=args.check,
            notes=notes,
            install=install,
            owner=owner,
            machine=machine,
            codex_sandbox=args.codex_sandbox,
            codex_inline=args.codex_inline,
        )
    )


def ask_machine_label(root: Path) -> str | None:
    """Ask for this machine's dispatch label while `agent.local.yaml` has none. Enter takes the
    hostname-derived default; an answer dispatch would refuse is asked for again with the rule.
    A label already set is never asked for."""
    from ..dispatch import machine as machine_mod

    if machine_mod.resolve(root).tag is not None or machine_mod.configured(root) is not machine_mod.ABSENT:
        return None
    default = machine_mod.default_label()
    print(
        "Each machine needs a short label that tags its dispatch ids, so two machines sharing one\n"
        f"notes repo never collide. It is {machine_mod.RULE}."
    )
    while True:
        answer = input(f"This machine's name [{default}]: ").strip().lower() or default
        problem = machine_mod.label_problem(answer)
        if problem is None:
            return answer
        print(f"{answer!r} is not valid: {problem}.")


def ask_owner_name(root: Path, notes: str | None) -> str | None:
    """Ask for an absent owner name; Enter skips until a later run. Preserve an existing name and read
    the notes configuration before setup links it into a fresh clone.
    """
    from .. import config_file
    from .. import notes as notes_mod

    notes_root = (
        Path(notes).expanduser()
        if notes
        else notes_mod.read_notes_root(root) or notes_mod.default_notes_root(root)
    )
    if config_file.owner_name(root) or config_file.owner_name(Path(notes_root)):
        return None
    answer = input("Your name, for agent.yaml (Enter to skip): ").strip()
    return answer or None


def ask_notes_path(root: Path) -> str | None:
    """On a first run only, where the private notes repo goes. Enter keeps `../Agent-notes`;
    a path is either an existing notes repo to link or a new folder to create. A later run reuses the remembered repo and asks nothing."""
    from .. import notes as notes_mod

    if notes_mod.read_notes_root(root) is not None:
        return None
    default = notes_mod.default_notes_root(root)
    # "create it" was printed over an existing repo after a reclone.
    verb = "use the existing one" if (default / ".git").exists() else "create it"
    print(
        "Your notes repo holds your features, journal and agent.yaml, separate from this clone.\n"
        f"Press Enter to {verb} at {default},\n"
        "or type a path: an existing notes repo to use, or a new folder to create."
    )
    answer = input("Notes repo: ").strip()
    return answer or None


HANDLERS = {
    ("capture", None): capture_cmd,
    ("obsidian", "setup"): obsidian_setup,
    ("obsidian", "open"): obsidian_open,
    ("obsidian", "daily"): obsidian_daily,
    ("obsidian", "search"): obsidian_search,
    ("hook", None): hook_cmd,
    ("mcp", None): mcp_cmd,
    ("doctor", None): doctor_cmd,
    ("setup", None): setup_cmd,
}
