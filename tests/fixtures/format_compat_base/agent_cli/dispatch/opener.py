# ruff: noqa
from __future__ import annotations
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any
from ..core import AgentError
from .state import load

OBSIDIAN_EVAL_TIMEOUT_S = 10.0
SHELL_COMMAND_ID = "agent-worker-open"


def user_shell() -> str:
    shell = os.environ.get("SHELL", "").strip()
    if shell and Path(shell).is_absolute() and Path(shell).is_file():
        return shell
    if sys.platform == "darwin":
        return "/bin/zsh"
    return "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"


def open_command(root: Path, d: dict[str, Any]) -> tuple[str, str]:
    if d.get("state") in ("queued", "running"):
        return (str(root), f"bin/agent dispatch watch {shlex.quote(str(d['id']))} --follow")
    if not d.get("session_id"):
        raise AgentError(f"dispatch {d['id']} has no worker session to resume")
    worktree = Path(str(d.get("worktree") or ""))
    cwd = str(worktree) if worktree.is_dir() else str(root)
    return (cwd, f"claude --resume {shlex.quote(str(d['session_id']))}")


VAULT_KEY = "obsidian_vault"


def vault_name(root: Path) -> str:
    from ..config_file import machine_root, machine_section

    configured = machine_section(root, VAULT_KEY)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return machine_root(root).name


def open_uri(root: Path, dispatch_id: str) -> str:
    from urllib.parse import quote

    vault = quote(vault_name(root), safe="")
    return f"obsidian://shell-commands/?vault={vault}&execute={SHELL_COMMAND_ID}&_dispatch={dispatch_id}"


def obsidian_cli() -> str | None:
    from ..obsidian import cli_path

    return cli_path()


def _eval_code(title: str, cwd: str, command: str) -> str:
    shell = user_shell()
    profile = {
        "type": "integrated",
        "executable": shell,
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
    state = {
        "cwd": cwd,
        "focus": True,
        "profile": profile,
        "profileSourceId": None,
        "serial": None,
        "userTitle": title,
    }
    view = {"type": "terminal:terminal", "active": True, "state": {"terminal:terminal": state}}
    return f"(async () => {{ const leaf = app.workspace.getLeaf('split'); await leaf.setViewState({json.dumps(view)}); return 'opened'; }})()"


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
    from .. import obsidian_tabs

    title = obsidian_tabs.worker_label(str(d["id"]), d.get("state")) if mode == "watch" else ""
    code = _eval_code(title, cwd, command)
    from .. import obsidian_run

    if not obsidian_run.launch():
        out["opened"], out["reason"] = (False, "Obsidian did not start")
        return out
    from ..tab_drain import run_eval

    run = run_eval(root, code, OBSIDIAN_EVAL_TIMEOUT_S, source="open")
    if run.ended in ("timeout", "oserror", "lock-busy"):
        detail = run.stderr or (
            "another Obsidian eval held the lock" if run.ended == "lock-busy" else "timed out"
        )
        out["opened"], out["reason"] = (False, f"obsidian eval failed ({run.ended}): {detail}")
        return out
    if run.ended == "not-running":
        out["opened"], out["reason"] = (False, "Obsidian closed before the tab opened")
        return out
    out["opened"] = run.stdout is not None and "opened" in run.stdout
    if not out["opened"]:
        out["reason"] = run.stderr or (run.stdout or "").strip()[-300:] or f"exit {run.returncode}"
    return out
