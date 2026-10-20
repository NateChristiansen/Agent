# ruff: noqa
from __future__ import annotations
import os
import shutil
from collections.abc import Mapping
from pathlib import Path


def _npm_entry(shim: Path) -> Path:
    return shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    if os.name != "nt":
        return env
    home = env.get("CODEX_HOME")
    if not home:
        profile = env.get("USERPROFILE")
        if not profile:
            drive, path = (env.get("HOMEDRIVE"), env.get("HOMEPATH"))
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
                f"Codex batch shim at {executable} has no adjacent npm script or Node; install the native executable or repair the npm installation"
            )
        return [node, str(script), *args]
    if os.name == "nt" and executable.suffix.lower() == ".ps1":
        raise FileNotFoundError("Codex resolved to a PowerShell script; use codex.exe or npm's codex.cmd")
    return [found, *args]
