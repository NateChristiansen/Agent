"""What `agent setup` installs after the owner's one yes, and the terminal plugin's Python.
`setup` holds the requirement table and the checks. This module holds what acts on them:
finding the Obsidian app, choosing which missing tools can be installed unattended, running the
installs in order, reloading PATH on Windows after winget, and the private venv the Obsidian
Terminal plugin needs on Windows. Nothing here runs without the owner's yes; the CLI asks.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import setup as setup_mod
from .core import agent_path
from .setup import MACOS, TOOLS, WINDOWS, Tool

# The Obsidian Terminal plugin's integrated terminal on Windows runs a Python helper that needs
# these, per the plugin's own `src/python-requirements.json` at the pinned 3.27.2 (win32 only).
# Setup puts them in a private venv so the owner's system Python is never touched.
TERMINAL_PYTHON_DIR = agent_path(Path(), "terminal-python")
TERMINAL_PYTHON_PACKAGES = ("psutil>=5.9.5", "pywinctl>=0.0.50", "typing_extensions>=4.7.1")


def obsidian_app_found(system: str) -> str:
    """Where the Obsidian app is installed, or "" when it is not. The desktop app has no
    binary on PATH by default, so this looks where each OS's installers put it."""
    home = Path.home()
    if system == MACOS:
        candidates = [Path("/Applications/Obsidian.app"), home / "Applications" / "Obsidian.app"]
    elif system == WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        # The protocol handler first: every installer registers `obsidian://`, wherever it put
        # the app. The owner's install was at %LOCALAPPDATA%\Obsidian, which no folder guess
        # had covered.
        registered = _obsidian_protocol_exe()
        candidates = [
            *([registered] if registered else []),
            local / "Obsidian" / "Obsidian.exe",
            local / "Programs" / "Obsidian" / "Obsidian.exe",
            program_files / "Obsidian" / "Obsidian.exe",
        ]
    else:
        found = shutil.which("obsidian") or shutil.which("Obsidian")
        if found:
            return found
        candidates = [
            Path("/var/lib/flatpak/app/md.obsidian.Obsidian"),
            home / ".local" / "share" / "flatpak" / "app" / "md.obsidian.Obsidian",
            Path("/snap/obsidian"),
        ]
    return next((str(p) for p in candidates if p.exists()), "")


def _protocol_command() -> str:
    """The `obsidian://` open command from the current user's or the machine's registry, or ""."""
    if sys.platform != "win32":
        return ""
    import winreg

    key = r"Software\Classes\obsidian\shell\open\command"
    for hive, path in ((winreg.HKEY_CURRENT_USER, key), (winreg.HKEY_CLASSES_ROOT, key.split("\\", 2)[2])):
        try:
            with winreg.OpenKey(hive, path) as handle:
                return str(winreg.QueryValueEx(handle, "")[0])
        except OSError:
            continue
    return ""


def _obsidian_protocol_exe() -> Path | None:
    """The executable in the `obsidian://` open command, such as
    `"C:\\Users\\me\\AppData\\Local\\Obsidian\\Obsidian.exe" "%1"`."""
    command = _protocol_command().strip()
    if not command:
        return None
    exe = command[1:].split('"', 1)[0] if command.startswith('"') else command.split(" ", 1)[0]
    return Path(exe) if exe.lower().endswith(".exe") else None


def _auto_argv(tool: Tool, system: str) -> tuple[str, ...]:
    if system == WINDOWS:
        return tool.auto_windows
    return tool.auto_macos if system == MACOS else tool.auto_linux


def installable(rows: list[dict[str, Any]], system: str) -> list[Tool]:
    """The missing tools setup can install unattended on `system`, in install order. An absent
    client is not offered while the other one is installed."""
    missing = {r["name"] for r in rows if not r["ok"]}
    if setup_mod.client_found(rows):
        missing -= set(setup_mod.CLIENTS)
    node = next(t for t in TOOLS if t.name == "node")

    def can(tool: Tool) -> bool:
        if tool.auto(system):
            return True
        # Claude installs through npm. When node is missing too, npm is not on PATH yet but
        # will be once node installs first, so it still counts.
        argv = _auto_argv(tool, system)
        return bool(argv) and argv[0] == "npm" and "node" in missing and bool(node.auto(system))

    # The Obsidian app is detected and guided, never installed: the agreed plan leaves
    # installing it to the owner. Its row still makes setup incomplete.
    return [t for t in TOOLS if t.name in missing and can(t)]


def refresh_windows_path() -> None:
    """Re-read PATH from the registry after winget installs. A new install lands in the
    machine or user PATH, which this process's environment does not see until it reloads."""
    if sys.platform != "win32":
        return
    import winreg

    parts: list[str] = []
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
                parts += [os.path.expandvars(p) for p in str(value).split(";") if p]
        except OSError:
            continue
    merged = dict.fromkeys(p for p in [*parts, *os.environ.get("PATH", "").split(";")] if p)
    os.environ["PATH"] = ";".join(merged)


def _working(tool: Tool) -> str:
    """The tool's path when it is on PATH and `<tool> --version` exits 0, else ""."""
    found = shutil.which(tool.binary)
    return found if found and setup_mod.run_ok([found, "--version"]) else ""


def install_missing(tools: list[Tool], system: str) -> list[dict[str, Any]]:
    """Install each tool in order after the owner's yes. One row per tool: `installed` or
    `failed` with the command's last line. Never raises; a failure keeps its guide line."""
    rows: list[dict[str, Any]] = []
    for tool in tools:
        argv = tool.auto(system)
        if not argv:
            detail = f"no installer on PATH; {tool.guide(system)}"
            rows.append({"tool": tool.name, "state": "failed", "detail": detail})
            continue
        try:
            proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=1800)
            ok = proc.returncode == 0
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = " ".join(argv) if ok else (tail[-1] if tail else f"exit {proc.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            ok, detail = False, str(exc)
        if system == WINDOWS:
            refresh_windows_path()
        # A working tool is the result, not the installer's exit code. winget exits non-zero
        # with "No newer package versions" for a package already installed, and an installer can exit 0 and leave nothing runnable on PATH.
        working = _working(tool)
        if working and not ok:
            detail = f"already installed: {working}"
        elif not working and ok:
            detail = (
                f"{' '.join(argv)} exited 0, but `{tool.binary} --version` does not run; {tool.guide(system)}"
            )
        rows.append({"tool": tool.name, "state": "installed" if working else "failed", "detail": detail})
    return rows


def terminal_python_path(root: Path, system: str | None = None) -> Path:
    """The interpreter of the terminal plugin's private venv. `obsidian setup` points the
    Windows terminal profile's `pythonExecutable` here."""
    base = root / TERMINAL_PYTHON_DIR
    if (system or platform.system()) == WINDOWS:
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def terminal_python(root: Path, system: str | None = None) -> dict[str, Any]:
    """Make the terminal plugin's venv with its Windows packages. Only Windows needs it: the
    plugin's Python helper runs there alone. Idempotent: an existing venv whose packages import
    is `already`."""
    system = system or platform.system()
    if system != WINDOWS:
        return setup_mod.setup_step("terminal python", "already", "not needed on this OS")
    python = terminal_python_path(root, system)
    probe = [str(python), "-c", "import psutil, pywinctl, typing_extensions"]
    if python.exists() and setup_mod.run_ok(probe):
        return setup_mod.setup_step("terminal python", "already", str(python))
    venv = root / TERMINAL_PYTHON_DIR
    if not python.exists() and not setup_mod.run_ok(["uv", "venv", str(venv)]):
        return setup_mod.setup_step("terminal python", "failed", f"uv venv {venv}")
    install = ["uv", "pip", "install", "--python", str(python), *TERMINAL_PYTHON_PACKAGES]
    if not setup_mod.run_ok(install):
        return setup_mod.setup_step("terminal python", "failed", " ".join(install))
    return setup_mod.setup_step("terminal python", "done", str(python))
