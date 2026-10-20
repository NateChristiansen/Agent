# ruff: noqa
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

TERMINAL_PYTHON_DIR = agent_path(Path(), "terminal-python")
TERMINAL_PYTHON_PACKAGES = ("psutil>=5.9.5", "pywinctl>=0.0.50", "typing_extensions>=4.7.1")


def obsidian_app_found(system: str) -> str:
    home = Path.home()
    if system == MACOS:
        candidates = [Path("/Applications/Obsidian.app"), home / "Applications" / "Obsidian.app"]
    elif system == WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        program_files = Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
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
    if sys.platform != "win32":
        return ""
    import winreg

    key = "Software\\Classes\\obsidian\\shell\\open\\command"
    for hive, path in ((winreg.HKEY_CURRENT_USER, key), (winreg.HKEY_CLASSES_ROOT, key.split("\\", 2)[2])):
        try:
            with winreg.OpenKey(hive, path) as handle:
                return str(winreg.QueryValueEx(handle, "")[0])
        except OSError:
            continue
    return ""


def _obsidian_protocol_exe() -> Path | None:
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
    missing = {r["name"] for r in rows if not r["ok"]}
    if setup_mod.client_found(rows):
        missing -= set(setup_mod.CLIENTS)
    node = next((t for t in TOOLS if t.name == "node"))

    def can(tool: Tool) -> bool:
        if tool.auto(system):
            return True
        argv = _auto_argv(tool, system)
        return bool(argv) and argv[0] == "npm" and ("node" in missing) and bool(node.auto(system))

    return [t for t in TOOLS if t.name in missing and can(t)]


def refresh_windows_path() -> None:
    if sys.platform != "win32":
        return
    import winreg

    parts: list[str] = []
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
                parts += [os.path.expandvars(p) for p in str(value).split(";") if p]
        except OSError:
            continue
    merged = dict.fromkeys((p for p in [*parts, *os.environ.get("PATH", "").split(";")] if p))
    os.environ["PATH"] = ";".join(merged)


def _working(tool: Tool) -> str:
    found = shutil.which(tool.binary)
    return found if found and setup_mod.run_ok([found, "--version"]) else ""


def install_missing(tools: list[Tool], system: str) -> list[dict[str, Any]]:
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
            detail = " ".join(argv) if ok else tail[-1] if tail else f"exit {proc.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            ok, detail = (False, str(exc))
        if system == WINDOWS:
            refresh_windows_path()
        working = _working(tool)
        if working and (not ok):
            detail = f"already installed: {working}"
        elif not working and ok:
            detail = (
                f"{' '.join(argv)} exited 0, but `{tool.binary} --version` does not run; {tool.guide(system)}"
            )
        rows.append({"tool": tool.name, "state": "installed" if working else "failed", "detail": detail})
    return rows


def terminal_python_path(root: Path, system: str | None = None) -> Path:
    base = root / TERMINAL_PYTHON_DIR
    if (system or platform.system()) == WINDOWS:
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def terminal_python(root: Path, system: str | None = None) -> dict[str, Any]:
    system = system or platform.system()
    if system != WINDOWS:
        return setup_mod.setup_step("terminal python", "already", "not needed on this OS")
    python = terminal_python_path(root, system)
    probe = [str(python), "-c", "import psutil, pywinctl, typing_extensions"]
    if python.exists() and setup_mod.run_ok(probe):
        return setup_mod.setup_step("terminal python", "already", str(python))
    venv = root / TERMINAL_PYTHON_DIR
    if not python.exists() and (not setup_mod.run_ok(["uv", "venv", str(venv)])):
        return setup_mod.setup_step("terminal python", "failed", f"uv venv {venv}")
    install = ["uv", "pip", "install", "--python", str(python), *TERMINAL_PYTHON_PACKAGES]
    if not setup_mod.run_ok(install):
        return setup_mod.setup_step("terminal python", "failed", " ".join(install))
    return setup_mod.setup_step("terminal python", "done", str(python))
