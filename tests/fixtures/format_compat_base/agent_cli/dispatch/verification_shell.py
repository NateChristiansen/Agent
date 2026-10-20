# ruff: noqa
from __future__ import annotations
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from ..core import AgentError
from ..launcher import is_windows

CLAUDE_BASH = "CLAUDE_CODE_GIT_BASH_PATH"
CARRIED = "AGENT_VERIFIER_BASH"
_EXEC_PATH_TIMEOUT = 10


def _named(environ: Mapping[str, str], name: str) -> str | None:
    folded = {key.upper(): value for key, value in environ.items()}
    return folded.get(name.upper()) or None


def _search_path(environ: Mapping[str, str]) -> str | None:
    return _named(environ, "PATH")


def _windows_text(path: Path) -> str:
    text = str(path).replace("/", "\\")
    for prefix in ("\\\\?\\UNC\\", "\\\\?\\", "\\\\.\\"):
        if text.upper().startswith(prefix.upper()):
            return ("\\\\" if prefix.endswith("UNC\\") else "") + text[len(prefix) :]
    return text


def _windows_launcher(path: Path, environ: Mapping[str, str]) -> bool:
    root = _windows_text(Path(_named(environ, "SYSTEMROOT") or "C:\\Windows")).lower().rstrip("\\")
    text = _windows_text(path).lower()
    return text.startswith(root + "\\") or "\\windowsapps\\" in text


def _from_exec_path(environ: Mapping[str, str]) -> tuple[Path | None, str]:
    git = shutil.which("git", path=_search_path(environ))
    if git is None:
        return (None, "no git on PATH to ask for `git --exec-path`")
    try:
        result = subprocess.run(
            [git, "--exec-path"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(environ),
            timeout=_EXEC_PATH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (None, f"`{git} --exec-path` failed: {exc}")
    exec_path = result.stdout.strip()
    if result.returncode != 0 or not exec_path:
        return (None, f"`{git} --exec-path` exited {result.returncode}")
    for parent in Path(exec_path).parents:
        candidate = parent / "bin" / "bash.exe"
        if candidate.is_file():
            return (candidate, "")
    return (None, f"no bin\\bash.exe above the Git exec path {exec_path}")


def _not_git_bash(path: Path, environ: Mapping[str, str]) -> str:
    if PureWindowsPath(_windows_text(path)).name.lower() != "bash.exe":
        return f"{path} is not a bash.exe"
    if _windows_launcher(path, environ):
        return f"{path} starts WSL, not Git Bash"
    if not path.is_file():
        return f"{path} is not a file"
    if _windows_launcher(path.resolve(), environ):
        return f"{path} leads to {path.resolve()}, which starts WSL, not Git Bash"
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(environ),
            timeout=_EXEC_PATH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"`{path} --version` failed: {exc}"
    first = (result.stdout.splitlines() or [""])[0]
    if result.returncode != 0 or not first.startswith("GNU bash"):
        return f"`{path} --version` does not report GNU bash"
    return ""


def find_bash(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    for name in (CLAUDE_BASH, CARRIED):
        value = _named(environ, name)
        if value is not None:
            why = _not_git_bash(Path(value), environ)
            if not why:
                return Path(value)
            raise AgentError(
                f"supervisor verification on Windows needs Git Bash, and {name} names {value}: {why}; point it at Git for Windows bash.exe"
            )
    found, why = _from_exec_path(environ)
    if found is not None:
        refused = _not_git_bash(found, environ)
        if not refused:
            return found
        why = refused
    on_path = shutil.which("bash", path=_search_path(environ))
    path_why = _not_git_bash(Path(on_path), environ) if on_path else "no bash.exe on PATH"
    if on_path is not None and (not path_why):
        return Path(on_path)
    raise AgentError(
        f"supervisor verification on Windows needs Git for Windows bash.exe and found none: {CLAUDE_BASH} is unset, {why}, and {path_why}. Install Git for Windows or set {CLAUDE_BASH}."
    )


def carried(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    if not is_windows():
        return {}
    try:
        return {CARRIED: str(find_bash(environ))}
    except AgentError:
        return {}


def find_git(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    git = shutil.which("git", path=_search_path(environ))
    if git is None:
        raise AgentError("system Git is required for verification")
    return str(Path(git).resolve())
