"""Resolve one Codex launch command for every manager and worker call site.

On Windows an npm install usually exposes ``codex.cmd``. Windows may run batch files through
cmd.exe even when Python asks for ``shell=False``, so a JSON argument can be reparsed by a
shell. Resolve the package's JavaScript entry and run it through Node instead. Prefer a real
``codex.exe`` when one is on PATH. Never silently pass protocol text through a .cmd shim.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path


def _npm_entry(shim: Path) -> Path:
    return shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Give a Windows Codex child an explicit home without changing this process."""
    env = dict(os.environ if base is None else base)
    if os.name != "nt":
        return env
    home = env.get("CODEX_HOME")
    if not home:
        profile = env.get("USERPROFILE")
        if not profile:
            drive, path = env.get("HOMEDRIVE"), env.get("HOMEPATH")
            profile = f"{drive}{path}" if drive and path else None
        if not profile:
            raise FileNotFoundError("Codex home is unresolved; set CODEX_HOME or USERPROFILE")
        candidate = Path(profile) / ".codex"
        if not candidate.is_dir():
            return env
        home = str(candidate)
    env["CODEX_HOME"] = home.replace("\\", "/")
    return env


def command(*args: str) -> list[str]:
    """An argv for Codex, or FileNotFoundError with a setup action."""
    if os.name == "nt":
        native = shutil.which("codex.exe")
        if native:
            return [native, *args]
    found = shutil.which("codex")
    if not found:
        raise FileNotFoundError("Codex is not on PATH; install it and run setup again")
    executable = Path(found)
    if os.name == "nt" and executable.suffix.lower() in {".cmd", ".bat"}:
        script = _npm_entry(executable)
        node = shutil.which("node.exe") or shutil.which("node")
        if not script.is_file() or not node:
            raise FileNotFoundError(
                f"Codex batch shim at {executable} has no adjacent npm script or Node; "
                "install the native executable or repair the npm installation"
            )
        return [node, str(script), *args]
    if os.name == "nt" and executable.suffix.lower() == ".ps1":
        raise FileNotFoundError("Codex resolved to a PowerShell script; use codex.exe or npm's codex.cmd")
    return [found, *args]
