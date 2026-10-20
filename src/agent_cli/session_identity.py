"""Resolve session id, client pid and provider for session records, pid maps, leases and views.
`sessions.py` re-exports the public helpers. Pid and provider share one bounded ancestor walk so
they identify the same client.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .core import AgentError

# ------------------------------------------------------------------ whose pid is this session?
# One answer, three callers. `hooks._resolve_pid`, `ops._resolve_pid` and
# `mcp_server._candidate_pids` each had their own copy. Each docstring claimed the others
# agreed with it. They now call these. The order is CLAUDE_PID when Claude Code
# exports it, else our parent process (Claude spawns hooks and the MCP server directly), else
# its parent (a shell sitting in between).


def _psutil_process(pid: int):  # noqa: ANN202 — psutil is a win32-only dependency
    import importlib

    try:
        return importlib.import_module("psutil").Process(pid)
    except Exception:  # noqa: BLE001 — gone, denied, or psutil missing: no answer
        return None


def _parent_pid(pid: int) -> int | None:
    if sys.platform == "win32":
        # No `ps` on Windows. The owner's Windows handoff found every Codex
        # record holding a short-lived pid: this walk returned nothing there, so the fallback
        # recorded the hook's own parent.
        proc = _psutil_process(pid)
        try:
            return int(proc.ppid()) if proc is not None else None
        except Exception:  # noqa: BLE001
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
    """Every pid that could own this session, best first. Used by the MCP server, which has no
    session id of its own and looks each one up in `.agent/sessions/by-pid/`.

    Reads this session's client's pid variable, not `CLAUDE_PID` outright: a Codex session
    started from a Claude terminal inherits that one and would look up the outer session.
    """
    provider = resolve_provider()
    for var in PROVIDER_PID_VARS.get(provider, ()):
        env = os.environ.get(var)
        if env and env.isdigit():
            return [int(env)]
    parent = os.getppid()
    out: list[int] = []
    # The client itself comes first, because that is the pid `resolve_pid` writes into the
    # by-pid map for a client that exports none. Without it the MCP server in a Codex session
    # looked up the shell that started it and found nothing, while the entry sat under the
    # Codex process's pid.
    found = client_process(parent)
    if found and found[1] == provider:
        out.append(found[0])
    if parent not in out:
        out.append(parent)
    grand = _parent_pid(parent)
    if grand and grand > 1 and grand not in out:
        out.append(grand)
    return out


CLIENT_HINTS = ("codex", "claude")
"""Executable names that identify a client process, for a session whose client exports no pid.

Matched as the exact basename of a token in the command line, never as a substring of the whole
line. A hook's own process is `/bin/sh -c` running `bin/agent hook codex-activity`, whose command
line contains "codex":
a substring match stopped the walk at the short-lived shell that ran the hook, which is the very
process whose death caused the stale pid. Tokens rather than argv[0] because a client can be
launched through an interpreter — `node /opt/homebrew/bin/codex` is the real Codex parent, and
its argv[0] is `node`.
"""


_INTERPRETERS = ("node", "bun", "deno")
_WINDOWS_SUFFIXES = (".exe", ".cmd", ".js")


def _base(token: str) -> str:
    """A token's executable name on any OS: `C:\\...\\codex.exe` and npm's `codex.js` shim are
    `codex`, like `/opt/homebrew/bin/codex`. Both separators, because the walk reads Windows
    command lines on Windows and tests read them everywhere."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in _WINDOWS_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _client_at(tokens: list[str]) -> int | None:
    """The index of the client in executable position: the executable itself, or the script
    an interpreter runs (`node /opt/homebrew/bin/codex`). A client name anywhere else is only
    text, as in `/bin/sh -c codex resume <id>` or `grep codex`."""
    start = 1 if tokens and _base(tokens[0]) in _INTERPRETERS else 0
    return start if len(tokens) > start and _base(tokens[start]) in CLIENT_HINTS else None


def _client_name(command: str) -> str | None:
    """Which of `CLIENT_HINTS` this command line runs, or None."""
    tokens = command.split()
    at = _client_at(tokens)
    return _base(tokens[at]) if at is not None else None


def _is_shared_server(command: str) -> bool:
    """A client process that serves many sessions: `codex app-server` (the per-user managed
    daemon on Codex 0.157, pid 321 on the owner's Mac). Its pid is no session's pid."""
    return "app-server" in command.split()


def _windows_command(argv: list[str]) -> str:
    """One Windows argv as a space-separated line whose tokens survive `split()`: a space inside
    a token (`C:\\Program Files\\nodejs\\node.exe`) is written `%20`."""
    return " ".join(arg.replace(" ", "%20") for arg in argv)


def _command(pid: int) -> str:
    if sys.platform == "win32":
        proc = _psutil_process(pid)
        try:
            return _windows_command(list(proc.cmdline())) if proc is not None else ""
        except Exception:  # noqa: BLE001
            return ""
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    return proc.stdout.strip()


def pid_running_session(session_id: str) -> int | None:
    """Find a live client resuming the exact session UUID, or None. Codex resume sends no
    SessionStart, so process discovery keeps quiet resumed managers reachable before their first
    prompt. Read the process table once for all candidates.
    """
    if not session_id or os.environ.get("AGENT_NOW"):
        return None
    if sys.platform == "win32":
        import importlib

        try:
            rows = list(importlib.import_module("psutil").process_iter(["pid", "cmdline"], ad_value=None))
        except Exception:  # noqa: BLE001
            return None
        for row in rows:
            argv = row.info.get("cmdline") or []
            if argv and _resumes(_windows_command(list(argv)), session_id):
                return int(row.info["pid"])
        return None
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
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
    """`<client> resume <session_id>`, as tokens. Not the id anywhere in the line: a Codex tool
    call runs under the codex binary with the command text on its argv, so any command that
    mentions the thread id matched, and a Codex manager recorded a finished tool process as its
    own pid."""
    tokens = command.split()
    at = _client_at([t.lower() for t in tokens])
    if at is None or _is_shared_server(command.lower()):
        return False
    # `resume` must be the subcommand: the first argument after the client that is not a
    # flag. `codex sandbox -- zsh -c resume <id>` runs a sandboxed command, not a resume.
    args = [t for t in tokens[at + 1 :] if not t.startswith("-")]
    return bool(args) and args[0] == "resume" and session_id in args[1:]


def client_process(start: int, limit: int = 6, *, shared: bool = False) -> tuple[int, str] | None:
    """Find the nearest client ancestor as `(pid, name)` in one walk. Hook shells are short-lived,
    while login shells and terminals outlive sessions; bound the walk and accept only a client
    process.
    """
    if os.environ.get("AGENT_NOW"):
        # Off under the test clock, like the notifier and the tab relabel. The suite runs under a
        # real client process, so the walk climbs out of the simulated environment and finds it:
        # `test_session_start_mapped` wrote its by-pid entry under the real client's pid and then
        # looked up the test's parent. That is the caller's machine
        # reaching into an isolated test, one level up from a cwd fallback; the fix is the same shape.
        return None
    pid = start
    for _ in range(limit):
        command = _command(pid).lower()
        name = _client_name(command)
        if name and _is_shared_server(command):
            # Codex's managed daemon runs hooks for every thread and outlives each one, so it
            # owns no session; nothing above it does either. Asked only
            # which client this is (`shared=True`), it still answers: the daemon is Codex. A
            # Codex hook inherits Claude's variables when Codex was started from a Claude
            # terminal, and without this the label fell back to "claude" and the hook's
            # short-lived launcher was recorded as a proven pid.
            return (pid, name) if shared else None
        if name:
            return pid, name
        parent = _parent_pid(pid)
        if not parent or parent <= 1:
            return None
        pid = parent
    return None


def client_pid(start: int, limit: int = 6) -> int | None:
    """The pid half of `client_process`."""
    found = client_process(start, limit=limit)
    return found[0] if found else None


def resolve_identity() -> tuple[int | None, bool]:
    """Return `(pid, known)` with positive session association. Prefer the client's pid variable, then
    an exact resume command, then a client ancestor outside shared servers. Claude may fall back to
    its parent; Codex under a shared daemon returns `(None, False)` rather than guess.
    """
    try:
        provider = resolve_provider()
        for var in PROVIDER_PID_VARS.get(provider, ()):
            env = os.environ.get(var)
            if env and env.isdigit():
                return int(env), True
        if provider == "codex":
            thread = next((os.environ[v] for v in PROVIDER_SESSION_VARS["codex"] if os.environ.get(v)), "")
            running = pid_running_session(thread) if thread else None
            if running:
                return running, True
        parent = os.getppid()
        found = client_process(parent)
        # Only when the ancestor is this session's client. Finding the *other* client up the
        # tree means we are inside its terminal, not that it owns this session.
        if found and found[1] == provider:
            return found[0], True
        return (parent, True) if provider != "codex" else (None, False)
    except Exception:  # noqa: BLE001
        return None, False


def resolve_pid() -> int | None:
    """The pid half of `resolve_identity`: None when it is unknown.

    The single pid whose liveness stands in for this session's. Written to the session record
    and to the by-pid map by the session-start hook, and recorded as a lease holder by
    `ops.lease_take`; the record, the map and a lease all name the same process this way.

    The ancestor walk exists for clients that export no pid: the parent of a hook is the shell
    that ran it, which dies immediately, so a record written from it is stale before anything
    reads it. Keyed on the provider rather than reading `CLAUDE_PID` outright, because a Codex
    session started from a Claude terminal inherits it and would register the outer session's
    pid as its own liveness.
    """
    return resolve_identity()[0]


# ------------------------------------------------------------------ which session, which client

ENV_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
)
"""What each client exports to a shell it starts, in the order they are tried. Codex sets
`CODEX_SESSION_ID` and `CODEX_THREAD_ID` to the same value and a child inherits both (verified in a live Codex manager). Without them `resolve_session_id` raised for every
`bin/agent` call from a Codex session, so nothing registered and no Codex session could appear
as a manager. This is the first half of provider identity; the pid half is `resolve_pid`, which
already falls back to the parent process."""

PROVIDER_SESSION_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"),
    "codex": ("CODEX_SESSION_ID", "CODEX_THREAD_ID"),
}
PROVIDER_PID_VARS: dict[str, tuple[str, ...]] = {"claude": ("CLAUDE_PID",), "codex": ()}
CLAUDE_ENV_VARS = PROVIDER_SESSION_VARS["claude"] + PROVIDER_PID_VARS["claude"]
CODEX_ENV_VARS = PROVIDER_SESSION_VARS["codex"] + PROVIDER_PID_VARS["codex"]
DEFAULT_PROVIDER = "claude"

# Derive session id and pid from resolve_provider so inherited variables from an outer client
# cannot associate a nested session with the wrong owner.


def resolve_provider() -> str:
    """Resolve claude or codex from client environment variables; when both are inherited, use the
    nearest client ancestor. Skip ancestry otherwise because activity runs on every tool event and
    a walk costs up to six ps calls. Default to claude rather than fail a hook over a missing
    label.
    """
    try:
        claude = any(os.environ.get(var) for var in CLAUDE_ENV_VARS)
        codex = any(os.environ.get(var) for var in CODEX_ENV_VARS)
        if claude != codex:
            return "claude" if claude else "codex"
        found = client_process(os.getppid(), shared=True)
        if found:
            return found[1]
    except Exception:  # noqa: BLE001, S110 — a label is not worth failing a hook over
        pass
    return DEFAULT_PROVIDER


def session_env_vars(provider: str) -> tuple[str, ...]:
    """The session-id variables to try, this session's client first.

    The other client's come last rather than not at all: a variable inherited from an outer
    session is a poor answer, but it beats raising, and `ENV_VARS` is what the failure message
    lists. The order is the whole fix — reading a fixed order is what let an outer Claude
    session's id win inside a Codex one.
    """
    first = PROVIDER_SESSION_VARS.get(provider, ())
    rest = tuple(v for p, names in PROVIDER_SESSION_VARS.items() if p != provider for v in names)
    return first + rest


def resolve_session_id(root: Path, explicit: str | None = None) -> str:
    """`--session-id`, else the first session variable this session's own client exported.
    Reports the source on stderr. No fallback beyond that: the env var is what makes parallel
    sessions resolve to themselves."""
    if explicit:
        return explicit
    for var in session_env_vars(resolve_provider()):
        env = os.environ.get(var)
        if env:
            sys.stderr.write(f"session id: from {var}\n")
            return env
    # No `latest` or global-feature fallback. With several sessions open either silently picks
    # another session's work.
    raise AgentError(
        f"no session id: pass --session-id, or run inside a supported session ({', '.join(ENV_VARS)})."
    )
