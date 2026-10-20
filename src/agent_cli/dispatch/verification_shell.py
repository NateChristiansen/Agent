"""The programs a Windows supervisor verifier runs: Git for Windows `bash.exe` and `git.exe`.

`dispatch.verify` commands are sh syntax, so Windows runs them under `bash.exe -c`. The lookup
is supervisor-owned and grants a worker nothing. `detach` resolves bash where
`CLAUDE_CODE_GIT_BASH_PATH` is still visible and hands the result to the supervisor as
`CARRIED`, because the supervisor's environment drops every `CLAUDE*` name.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
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
    r"""`path` as a plain Windows path: forward slashes turned back, and the `\\?\` and `\\.\`
    device prefixes dropped, so `\\?\C:\Windows\System32\bash.exe` compares as System32."""
    text = str(path).replace("/", "\\")
    for prefix in ("\\\\?\\UNC\\", "\\\\?\\", "\\\\.\\"):
        if text.upper().startswith(prefix.upper()):
            return ("\\\\" if prefix.endswith("UNC\\") else "") + text[len(prefix) :]
    return text


def _windows_launcher(path: Path, environ: Mapping[str, str]) -> bool:
    """System32's `bash.exe` and the WindowsApps alias start WSL, not Git Bash."""
    root = _windows_text(Path(_named(environ, "SYSTEMROOT") or r"C:\Windows")).lower().rstrip("\\")
    text = _windows_text(path).lower()
    return text.startswith(root + "\\") or "\\windowsapps\\" in text


def _from_exec_path(environ: Mapping[str, str]) -> tuple[Path | None, str]:
    git = shutil.which("git", path=_search_path(environ))
    if git is None:
        return None, "no git on PATH to ask for `git --exec-path`"
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
        return None, f"`{git} --exec-path` failed: {exc}"
    exec_path = result.stdout.strip()
    if result.returncode != 0 or not exec_path:
        return None, f"`{git} --exec-path` exited {result.returncode}"
    for parent in Path(exec_path).parents:
        candidate = parent / "bin" / "bash.exe"
        if candidate.is_file():
            return candidate, ""
    return None, f"no bin\\bash.exe above the Git exec path {exec_path}"


def _not_git_bash(path: Path, environ: Mapping[str, str]) -> str:
    """Why `path` is not a usable Git Bash, or "" when it is: a file named `bash.exe`, not the
    WSL launcher, whose `--version` says GNU bash. Every verify command runs through it, so a
    stray file named by a variable must be refused at launch, not at the first check."""
    # A Windows path names its file by backslash on every platform the tests run on.
    if PureWindowsPath(_windows_text(path)).name.lower() != "bash.exe":
        return f"{path} is not a bash.exe"
    if _windows_launcher(path, environ):
        return f"{path} starts WSL, not Git Bash"
    if not path.is_file():
        return f"{path} is not a file"
    # A link named bash.exe that leads to the launcher still starts WSL.
    if _windows_launcher(path.resolve(), environ):
        return f"{path} leads to {path.resolve()}, which starts WSL, not Git Bash"
    checked = _CHECKED.get()
    if checked is None:
        return _version_refusal(path, environ)
    key = (str(path), tuple(sorted(environ.items())))
    if key not in checked:
        checked[key] = _version_refusal(path, environ)
    return checked[key]


_CHECKED: ContextVar[dict[tuple[object, ...], str] | None] = ContextVar(
    "verification_bash_checked", default=None
)


@contextmanager
def bash_scope() -> Generator[None]:
    """Within one request, run each bash's `--version` check once; nothing outlives it."""
    if _CHECKED.get() is not None:
        yield
        return
    token = _CHECKED.set({})
    try:
        yield
    finally:
        _CHECKED.reset(token)


def _version_refusal(path: Path, environ: Mapping[str, str]) -> str:
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
    """Git Bash for the verifier: `CLAUDE_CODE_GIT_BASH_PATH`, then what `detach` carried, then
    the Git install `git --exec-path` points into, then PATH. Each candidate must pass
    `_not_git_bash`. Raises naming every cause."""
    environ = os.environ if environ is None else environ
    for name in (CLAUDE_BASH, CARRIED):
        value = _named(environ, name)
        if value is not None:
            why = _not_git_bash(Path(value), environ)
            if not why:
                return Path(value)
            raise AgentError(
                f"supervisor verification on Windows needs Git Bash, and {name} names {value}: "
                f"{why}; point it at Git for Windows bash.exe"
            )
    found, why = _from_exec_path(environ)
    if found is not None:
        refused = _not_git_bash(found, environ)
        if not refused:
            return found
        why = refused
    on_path = shutil.which("bash", path=_search_path(environ))
    path_why = _not_git_bash(Path(on_path), environ) if on_path else "no bash.exe on PATH"
    if on_path is not None and not path_why:
        return Path(on_path)
    raise AgentError(
        f"supervisor verification on Windows needs Git for Windows bash.exe and found none: "
        f"{CLAUDE_BASH} is unset, {why}, and {path_why}. Install Git for Windows or set {CLAUDE_BASH}."
    )


def carried(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """What `detach` adds to the supervisor environment: the resolved bash on Windows when one
    exists, nothing otherwise. A checked launch has already refused when there is none."""
    if not is_windows():
        return {}
    try:
        return {CARRIED: str(find_bash(environ))}
    except AgentError:
        return {}


def find_git(environ: Mapping[str, str] | None = None) -> str:
    """The absolute `git.exe` the hardened verifier queries run, from the supervisor's PATH.
    The queries keep their restricted environment; only the executable comes from here."""
    environ = os.environ if environ is None else environ
    git = shutil.which("git", path=_search_path(environ))
    if git is None:
        raise AgentError("system Git is required for verification")
    return str(Path(git).resolve())
