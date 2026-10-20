# ruff: noqa
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from .core import AgentError


def _psutil_process(pid: int):
    import importlib

    try:
        return importlib.import_module("psutil").Process(pid)
    except Exception:
        return None


def _parent_pid(pid: int) -> int | None:
    if sys.platform == "win32":
        proc = _psutil_process(pid)
        try:
            return int(proc.ppid()) if proc is not None else None
        except Exception:
            return None
    try:
        proc = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=False, timeout=2
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    value = proc.stdout.strip()
    return int(value) if value.isdigit() else None


def candidate_pids() -> list[int]:
    provider = resolve_provider()
    for var in PROVIDER_PID_VARS.get(provider, ()):
        env = os.environ.get(var)
        if env and env.isdigit():
            return [int(env)]
    parent = os.getppid()
    out: list[int] = []
    found = client_process(parent)
    if found and found[1] == provider:
        out.append(found[0])
    if parent not in out:
        out.append(parent)
    grand = _parent_pid(parent)
    if grand and grand > 1 and (grand not in out):
        out.append(grand)
    return out


CLIENT_HINTS = ("codex", "claude")
'Executable names that identify a client process, for a session whose client exports no pid.\n\nMatched as the exact basename of a token in the command line, never as a substring of the whole\nline. A hook\'s own process is `/bin/sh -c` running `bin/agent hook codex-activity`, whose command\nline contains "codex":\na substring match stopped the walk at the short-lived shell that ran the hook, which is the very\nprocess whose death caused the stale pid. Tokens rather than argv[0] because a client can be\nlaunched through an interpreter — `node /opt/homebrew/bin/codex` is the real Codex parent, and\nits argv[0] is `node`.\n'
_INTERPRETERS = ("node", "bun", "deno")
_WINDOWS_SUFFIXES = (".exe", ".cmd", ".js")


def _base(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in _WINDOWS_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _client_at(tokens: list[str]) -> int | None:
    start = 1 if tokens and _base(tokens[0]) in _INTERPRETERS else 0
    return start if len(tokens) > start and _base(tokens[start]) in CLIENT_HINTS else None


def _client_name(command: str) -> str | None:
    tokens = command.split()
    at = _client_at(tokens)
    return _base(tokens[at]) if at is not None else None


def _is_shared_server(command: str) -> bool:
    return "app-server" in command.split()


def _windows_command(argv: list[str]) -> str:
    return " ".join((arg.replace(" ", "%20") for arg in argv))


def _command(pid: int) -> str:
    if sys.platform == "win32":
        proc = _psutil_process(pid)
        try:
            return _windows_command(list(proc.cmdline())) if proc is not None else ""
        except Exception:
            return ""
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False, timeout=2
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    return proc.stdout.strip()


def pid_running_session(session_id: str) -> int | None:
    if not session_id or os.environ.get("AGENT_NOW"):
        return None
    if sys.platform == "win32":
        import importlib

        try:
            rows = list(importlib.import_module("psutil").process_iter(["pid", "cmdline"], ad_value=None))
        except Exception:
            return None
        for row in rows:
            argv = row.info.get("cmdline") or []
            if argv and _resumes(_windows_command(list(argv)), session_id):
                return int(row.info["pid"])
        return None
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True, check=False, timeout=5
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        pid, _, command = stripped.partition(" ")
        if pid.isdigit() and _resumes(command, session_id):
            return int(pid)
    return None


def _resumes(command: str, session_id: str) -> bool:
    tokens = command.split()
    at = _client_at([t.lower() for t in tokens])
    if at is None or _is_shared_server(command.lower()):
        return False
    args = [t for t in tokens[at + 1 :] if not t.startswith("-")]
    return bool(args) and args[0] == "resume" and (session_id in args[1:])


def client_process(start: int, limit: int = 6, *, shared: bool = False) -> tuple[int, str] | None:
    if os.environ.get("AGENT_NOW"):
        return None
    pid = start
    for _ in range(limit):
        command = _command(pid).lower()
        name = _client_name(command)
        if name and _is_shared_server(command):
            return (pid, name) if shared else None
        if name:
            return (pid, name)
        parent = _parent_pid(pid)
        if not parent or parent <= 1:
            return None
        pid = parent
    return None


def client_pid(start: int, limit: int = 6) -> int | None:
    found = client_process(start, limit=limit)
    return found[0] if found else None


def resolve_identity() -> tuple[int | None, bool]:
    try:
        provider = resolve_provider()
        for var in PROVIDER_PID_VARS.get(provider, ()):
            env = os.environ.get(var)
            if env and env.isdigit():
                return (int(env), True)
        if provider == "codex":
            thread = next((os.environ[v] for v in PROVIDER_SESSION_VARS["codex"] if os.environ.get(v)), "")
            running = pid_running_session(thread) if thread else None
            if running:
                return (running, True)
        parent = os.getppid()
        found = client_process(parent)
        if found and found[1] == provider:
            return (found[0], True)
        return (parent, True) if provider != "codex" else (None, False)
    except Exception:
        return (None, False)


def resolve_pid() -> int | None:
    return resolve_identity()[0]


ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID")
"What each client exports to a shell it starts, in the order they are tried. Codex sets\n`CODEX_SESSION_ID` and `CODEX_THREAD_ID` to the same value and a child inherits both (verified in a live Codex manager). Without them `resolve_session_id` raised for every\n`bin/agent` call from a Codex session, so nothing registered and no Codex session could appear\nas a manager. This is the first half of provider identity; the pid half is `resolve_pid`, which\nalready falls back to the parent process."
PROVIDER_SESSION_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"),
    "codex": ("CODEX_SESSION_ID", "CODEX_THREAD_ID"),
}
PROVIDER_PID_VARS: dict[str, tuple[str, ...]] = {"claude": ("CLAUDE_PID",), "codex": ()}
CLAUDE_ENV_VARS = PROVIDER_SESSION_VARS["claude"] + PROVIDER_PID_VARS["claude"]
CODEX_ENV_VARS = PROVIDER_SESSION_VARS["codex"] + PROVIDER_PID_VARS["codex"]
DEFAULT_PROVIDER = "claude"


def resolve_provider() -> str:
    try:
        claude = any((os.environ.get(var) for var in CLAUDE_ENV_VARS))
        codex = any((os.environ.get(var) for var in CODEX_ENV_VARS))
        if claude != codex:
            return "claude" if claude else "codex"
        found = client_process(os.getppid(), shared=True)
        if found:
            return found[1]
    except Exception:
        pass
    return DEFAULT_PROVIDER


def session_env_vars(provider: str) -> tuple[str, ...]:
    first = PROVIDER_SESSION_VARS.get(provider, ())
    rest = tuple((v for p, names in PROVIDER_SESSION_VARS.items() if p != provider for v in names))
    return first + rest


def resolve_session_id(root: Path, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for var in session_env_vars(resolve_provider()):
        env = os.environ.get(var)
        if env:
            sys.stderr.write(f"session id: from {var}\n")
            return env
    raise AgentError(
        f"no session id: pass --session-id, or run inside a supported session ({', '.join(ENV_VARS)})."
    )
