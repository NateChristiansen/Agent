"""Open a worker tab according to dispatch state. Running or queued workers use `dispatch watch
--follow`: attaching another client would create a second transcript writer. Stopped workers or
those awaiting the owner open `claude --resume` in their worktree.

Obsidian eval sets the Terminal view profile and leaves a login shell afterward. Without the CLI or
running Obsidian, return a command to paste.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

from ..core import AgentError
from .state import load

OBSIDIAN_EVAL_TIMEOUT_S = 10.0
SHELL_COMMAND_ID = "agent-worker-open"


def user_shell() -> str:
    """The shell a `dispatch open` tab runs: the owner's own, `$SHELL`, when it names a file;
    else the platform's default, zsh on macOS and bash (or sh) elsewhere. It was `/bin/zsh` on
    every machine."""
    shell = os.environ.get("SHELL", "").strip()
    if shell and Path(shell).is_absolute() and Path(shell).is_file():
        return shell
    if sys.platform == "darwin":
        return "/bin/zsh"
    return "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"


# The Terminal plugin writes a Windows profile's args into a .bat, where `%` and `"` are read by
# cmd, and the orphan sweep in agent-terminal-focus matches the command line exactly.
WINDOWS_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
WORKER_LAUNCH_VAR = "AGENT_WORKER_LAUNCH"


def _ps_literal(value: str) -> str:
    if not WINDOWS_ID_RE.fullmatch(value):
        raise AgentError(f"{value!r} cannot be passed to a Windows terminal tab")
    return f"'{value}'"


def open_command(root: Path, d: dict[str, Any]) -> tuple[str, str]:
    """(cwd, shell command) for a dispatch record. On Windows the command is PowerShell."""
    windows = sys.platform == "win32"
    quote = _ps_literal if windows else shlex.quote
    if d.get("state") in ("queued", "running"):
        # PowerShell runs nothing from the cwd without a path; agent.cmd, unlike agent.ps1, runs
        # under the default execution policy.
        agent = "./bin/agent.cmd" if windows else "bin/agent"
        return str(root), f"{agent} dispatch watch {quote(str(d['id']))} --follow"
    if not d.get("session_id"):
        raise AgentError(f"dispatch {d['id']} has no worker session to resume")
    worktree = Path(str(d.get("worktree") or ""))
    cwd = str(worktree) if worktree.is_dir() else str(root)
    return cwd, f"claude --resume {quote(str(d['session_id']))}"


VAULT_KEY = "obsidian_vault"


def vault_name(root: Path) -> str:
    """Use machine-local obsidian_vault when set, otherwise main's folder name. Worktrees and probe
    names must not generate links to nonexistent vaults.
    """
    from ..config_file import machine_root, machine_section

    configured = machine_section(root, VAULT_KEY)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return machine_root(root).name


def open_uri(root: Path, dispatch_id: str) -> str:
    """The link a Workers row carries: runs the `agent-worker-open` Shell commands entry, which
    calls `bin/agent dispatch open <id>`."""
    from urllib.parse import quote

    vault = quote(vault_name(root), safe="")
    return f"obsidian://shell-commands/?vault={vault}&execute={SHELL_COMMAND_ID}&_dispatch={dispatch_id}"


def obsidian_cli() -> str | None:
    """`obsidian.cli_path`, the one resolver for the Obsidian CLI."""
    from ..obsidian import cli_path

    return cli_path()


def _windows_profile(root: Path, command: str) -> dict[str, Any]:
    """agent-terminal-focus's windowsManagerProfile, with a worker launch marker the plugin's
    orphan sweep recognises. -NoExit keeps the shell once the command ends."""
    from ..config_file import machine_root
    from ..installs import terminal_python_path
    from ..setup_tools import WINDOWS

    marker = f"worker:{uuid.uuid4()}"
    python = PureWindowsPath(terminal_python_path(machine_root(root), WINDOWS))
    return {
        "type": "integrated",
        "executable": "powershell.exe",
        "args": ["-NoLogo", "-NoExit", "-Command", f"$env:{WORKER_LAUNCH_VAR}='{marker}'; {command}"],
        "environment": [],
        "followTheme": True,
        "name": "",
        "platforms": {"win32": True},
        "pythonExecutable": str(python),
        "restoreHistory": False,
        "rightClickAction": "copyPaste",
        "successExitCodes": ["0", "SIGINT", "SIGTERM"],
        "terminalOptions": {"documentOverride": None},
        "useWin32Conhost": True,
    }


def _posix_profile(command: str) -> dict[str, Any]:
    shell = user_shell()
    return {
        "type": "integrated",
        "executable": shell,
        # -i as well as -l: `claude` is usually put on PATH in ~/.zshrc, which a login shell
        # running -c never reads. The first live click failed with "command not found: claude"
        # while the same shell, once interactive, ran it. `-l`, not `--login`,
        # because sh and dash take only the short form; zsh, bash and fish take both.
        "args": ["-i", "-l", "-c", f"{command}; exec {shlex.quote(shell)} -l"],
        "environment": [],
        "followTheme": True,
        "name": "",
        "platforms": {"darwin": True, "linux": True},
        "pythonExecutable": "python3",
        "restoreHistory": False,
        "rightClickAction": "copyPaste",
        "successExitCodes": ["0", "SIGINT", "SIGTERM"],
        "terminalOptions": {"documentOverride": None},
        "useWin32Conhost": True,
    }


def _eval_code(title: str, cwd: str, profile: dict[str, Any]) -> str:
    state = {
        "cwd": cwd,
        "focus": True,
        "profile": profile,
        "profileSourceId": None,
        "serial": None,
        "userTitle": title,
    }
    view = {"type": "terminal:terminal", "active": True, "state": {"terminal:terminal": state}}
    return (
        "(async () => { const leaf = app.workspace.getLeaf('split'); "
        f"await leaf.setViewState({json.dumps(view)}); return 'opened'; }})()"
    )


def open_(root: Path, *, dispatch_id: str, print_only: bool = False) -> dict[str, Any]:
    d = load(root, dispatch_id)
    cwd, command = open_command(root, d)
    mode = "watch" if d.get("state") in ("queued", "running") else "resume"
    out: dict[str, Any] = {
        "id": d["id"],
        "state": d.get("state"),
        "mode": mode,
        "cwd": cwd,
        "command": command,
    }
    obsidian = obsidian_cli()
    if print_only or obsidian is None:
        out["opened"] = False
        out["reason"] = "print only" if print_only else "obsidian CLI not on PATH"
        return out
    # A resume tab carries no label: Claude's own title (the worker's dispatch-id name with its
    # live ◐/✳ glyph) shows through. A watch tab has no Claude title, so it is labelled with
    # the same glyphs and relabelled as the worker's state changes (obsidian_tabs).
    from .. import obsidian_tabs

    title = obsidian_tabs.worker_label(str(d["id"]), d.get("state")) if mode == "watch" else ""
    profile = _windows_profile(root, command) if sys.platform == "win32" else _posix_profile(command)
    code = _eval_code(title, cwd, profile)
    from .. import obsidian_run

    # An explicit open may start Obsidian, but through the OS (`launch`), never by running the
    # CLI while the app is closed: that started it on our pipes.
    if not obsidian_run.launch():
        out["opened"], out["reason"] = False, "Obsidian did not start"
        return out
    from ..tab_drain import run_eval

    # Under the drainer's eval lock, beside no relabel batch, and recorded.
    run = run_eval(root, code, OBSIDIAN_EVAL_TIMEOUT_S, source="open")
    if run.ended in ("timeout", "oserror", "lock-busy"):
        detail = run.stderr or (
            "another Obsidian eval held the lock" if run.ended == "lock-busy" else "timed out"
        )
        out["opened"], out["reason"] = False, f"obsidian eval failed ({run.ended}): {detail}"
        return out
    if run.ended == "not-running":
        out["opened"], out["reason"] = False, "Obsidian closed before the tab opened"
        return out
    out["opened"] = run.stdout is not None and "opened" in run.stdout
    if not out["opened"]:
        out["reason"] = run.stderr or (run.stdout or "").strip()[-300:] or f"exit {run.returncode}"
    return out
