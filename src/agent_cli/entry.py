"""The `agent` console script: `agent hook <name>` without the rest of the CLI.

Every tool call fires a hook group, and each one is a fresh interpreter. `agent_cli.cli`
imports every subcommand, `dispatch` among them, and `dispatch` imports `claude_agent_sdk`.
A hook paid for all of that before reading its stdin. On Windows the
import alone took about 0.93 s per hook. This module routes a
plain `hook <name>` straight to `hooks.run` and imports the full CLI only for everything else.
Anything unusual about the arguments (`--probe`, a global flag, an unknown name) takes the
full CLI, so its errors and help stay the same.
"""

from __future__ import annotations

import sys


def utf8_streams() -> None:
    """Use UTF-8 output regardless of console defaults. Windows cp1252 cannot encode report glyphs
    such as the arrow.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and (stream.encoding or "").lower().replace("-", "") != "utf8":
            reconfigure(encoding="utf-8", errors="replace")


def _hook(name: str) -> int:
    """`agent hook <name>` as the full CLI runs it: fail open, print only what the hook says."""
    utf8_streams()
    try:
        from .core import find_root
        from .hooks.run import run

        text, code = run(find_root(None), name, sys.stdin.read())
    except Exception:  # noqa: BLE001 — an ordinary hook fails open, as it does in cli.main
        return 0
    if text:
        sys.stdout.write(text + "\n")
    return code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "hook":
        from .hooks.groups import GROUPS
        from .hooks.run import ALIASES, NAMES

        if args[1] in NAMES or args[1] in GROUPS or args[1] in ALIASES:
            return _hook(args[1])
    from .cli import main as cli_main

    return cli_main(argv)
