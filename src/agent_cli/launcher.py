from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core import AgentError

WINDOWS_ENTRY = Path(".venv") / "Scripts" / "python.exe"
#: After the interpreter: `-I` so the caller's cwd and PYTHONPATH never shadow the harness.
WINDOWS_MODULE = ["-I", "-m", "agent_cli"]
#: uv sync creates a Windows console shim that stays locked while a process runs it. Launch through
#: the interpreter to keep updates possible.
LEGACY_WINDOWS_ENTRY = Path(".venv") / "Scripts" / "agent.exe"


def is_windows() -> bool:
    return sys.platform == "win32"


def argv_prefix(root: Path, *, windows: bool | None = None) -> list[str]:
    """`bin/agent` on POSIX; on Windows the venv's `python.exe -I -m agent_cli`, not the
    `agent.exe` shim, which `uv sync` cannot replace while any process runs it."""
    if windows is None:
        windows = is_windows()
    return [str(root / WINDOWS_ENTRY), *WINDOWS_MODULE] if windows else [str(root / "bin" / "agent")]


def command_and_args(root: Path, *, windows: bool | None = None) -> tuple[str, list[str]]:
    """`argv_prefix` split for an MCP or hook config, which takes a command and its args apart."""
    first, *rest = argv_prefix(root, windows=windows)
    return first, rest


def spawn_supervisor(popen: Callable[..., Any], argv: list[str], **kwargs: Any) -> tuple[Any, bool | None]:
    """Start `argv` through `popen` in its own session (POSIX) or hidden in its own process group
    (Windows), and return the process with whether job breakaway was accepted (None off Windows)."""
    if sys.platform != "win32":
        return popen(argv, start_new_session=True, **kwargs), None
    hidden = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return popen(argv, creationflags=hidden | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs), True
    except OSError:
        return popen(argv, creationflags=hidden, **kwargs), False


def require(root: Path, *, windows: bool | None = None) -> None:
    if windows is None:
        windows = is_windows()
    if windows and not (root / WINDOWS_ENTRY).is_file():
        raise AgentError(f"{root / WINDOWS_ENTRY} is missing; run `uv sync` in {root} first")
