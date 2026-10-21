"""Verifier process mechanics; only inherited process groups are contained.

A guardian owns a watchdog pipe per command. The command cannot exec until its ownership
is durable. Guardian death closes the pipe and triggers cleanup without a Python finally.
Independent watchdog death fails closed; hostile same-UID tampering or setsid escape is not
contained. All subprocesses receive explicit environments and close unrelated descriptors.
On Windows a kill-on-close job named for the command's PID stands in for its process group
(`verification_win`), and the command runs under Git Bash instead of `/bin/sh`.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core import AgentError
from .verification_evidence import Binding, TreeState
from .verification_store import Store

if os.name == "nt":
    _ROOT = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    BASE_ENV = {"PATH": _ROOT + "\\System32", "LANG": "C", "LC_ALL": "C", "SYSTEMROOT": _ROOT}
else:
    BASE_ENV = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
POLL = 0.03
CLEANUP_SECONDS = 3.0
# Git for Windows sets this in the system config that verification ignores. Without it a CRLF
# checkout of LF blobs reads as tracked dirt; a repository's own core.autocrlf still wins.
WINDOWS_AUTOCRLF = "true"
_JOBS: dict[int, Any] = {}  # Windows: the guardian's handle to each live command job, by PID


class UnsupportedTree(AgentError):
    """A tree layout outside the initial bounded observer contract."""


def launch(
    role: str, *, env: dict[str, str], cwd: Path, stdout: Any = subprocess.DEVNULL
) -> subprocess.Popen[bytes]:
    if role not in {"guardian", "command", "watchdog"}:
        raise ValueError("invalid verifier process role")
    dispatch = Path(__file__).resolve().parent
    package, init = str(dispatch.parents[1]), str(dispatch / "__init__.py")
    module = "verification" if role == "guardian" else "verification_process"
    # The role registers `agent_cli.dispatch` without running its `__init__`: the facade's eager
    # imports more than double a cold start, and under load that outran the command deadline.
    code = (
        f"import sys, importlib.util as u; sys.path.insert(0, {package!r}); import agent_cli; "
        f"s = u.spec_from_file_location('agent_cli.dispatch', {init!r}, "
        f"submodule_search_locations=[{str(dispatch)!r}]); sys.modules[s.name] = u.module_from_spec(s); "
        f"from agent_cli.dispatch.{module} import {role}_main; {role}_main()"
    )
    if os.name == "nt":
        from .verification_win import launch_python

        process, job = launch_python(code, owned=role == "command", cwd=cwd, env=env, stdout=stdout)
        if job is not None:
            _JOBS[process.pid] = job
        return process
    return subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=subprocess.STDOUT if stdout == subprocess.PIPE else subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def send(process: subprocess.Popen[bytes], value: dict[str, Any]) -> None:
    if process.stdin is None:
        raise AgentError("verifier control pipe is unavailable")
    process.stdin.write((json.dumps(value, allow_nan=False) + "\n").encode())
    process.stdin.flush()


def close_control(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        process.stdin.close()


def readable(stream: Any, timeout: float) -> bool:
    """`select` on one pipe; Windows pipes cannot be selected, so they are peeked."""
    if os.name == "nt":
        from .verification_win import pipe_ready

        return pipe_ready(stream if isinstance(stream, int) else stream.fileno(), timeout)
    return bool(select.select([stream], [], [], timeout)[0])


def parent_gone() -> bool:
    if os.name == "nt":
        return readable(sys.stdin.fileno(), 0) and os.read(sys.stdin.fileno(), 1) == b""
    ready, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
    return bool(ready) and os.read(sys.stdin.fileno(), 1) == b""


def platform_tools() -> dict[str, str]:
    """The Windows request fields a guardian needs, resolved where the launch runs: Git Bash
    and an absolute git.exe. Nothing on POSIX."""
    if os.name != "nt":
        return {}
    from .verification_shell import find_bash, find_git

    return {"shell": str(find_bash()), "git": find_git()}


def system_git() -> str | None:
    if os.name == "nt":
        from .verification_shell import find_git

        try:
            return find_git()
        except AgentError:
            return None
    return shutil.which("git", path=os.defpath)


def release(pid: int) -> None:
    """Drop the guardian's handle to a command job; Windows ends anything still in it."""
    job = _JOBS.pop(pid, None)
    if job is not None:
        job.close()


def capture(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    limit: int = 8 * 1024 * 1024,
    *,
    own_group: bool = True,
) -> tuple[int, bytes]:
    """Bound time and bytes independently; never return stderr or interpolate it in errors."""
    if timeout <= 0:
        raise AgentError("verification observation deadline expired")
    # Fixed before the process starts, so a slow start counts against the caller's deadline.
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        from .verification_win import capture as capture_windows

        result = capture_windows(argv, cwd, env, timeout, limit, POLL)
        if time.monotonic() > deadline:
            raise AgentError("verification observation timed out")
        return result
    # POSIX preserves this real-time timer across exec. Trusted Git/ps self-expire even
    # if their Python parent is SIGKILLed and cannot execute its finally block.
    code = "import os,signal,sys; signal.signal(signal.SIGALRM,signal.SIG_DFL); signal.setitimer(signal.ITIMER_REAL,float(sys.argv[1])); os.execv(sys.argv[2],sys.argv[2:])"
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(timeout), *argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=own_group,
        close_fds=True,
    )
    output = bytearray()
    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentError("verification observation timed out")
            readable, _, _ = select.select([proc.stdout], [], [], min(POLL, remaining))
            if readable:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise AgentError("verification observation exceeded its output bound")
        return proc.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(output)
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL) if own_group else proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)
        if proc.stdout:
            proc.stdout.close()


def group_dead(pgid: int) -> bool:
    if os.name == "nt":
        # A guardian has no named job: its queries die with it (`verification_win.capture`).
        from . import verification_win as win

        try:
            return win.job_empty(pgid) and win.process_dead(pgid)
        except OSError:
            return False
    from .verification_status import absent  # the status module imports this one

    if absent(pgid, group=True):
        return True
    try:
        code, output = capture(["/bin/ps", "-axo", "pgid=,stat="], Path("/"), BASE_ENV, 1, 2 * 1024 * 1024)
        if code:
            return False
        for row in output.decode("ascii").splitlines():
            fields = row.split()
            if len(fields) != 2 or not fields[0].isdigit():
                return False
            if int(fields[0]) == pgid and not fields[1].startswith("Z"):
                return False
        return True
    except OSError, ValueError, AgentError, subprocess.TimeoutExpired:
        return False


def process_dead(pid: int | None) -> bool:
    if pid is None:
        return True
    if os.name == "nt":
        from .verification_win import process_dead as dead

        return dead(pid)
    from .verification_status import absent

    if absent(pid):
        return True
    try:
        code, output = capture(["/bin/ps", "-o", "stat=", "-p", str(pid)], Path("/"), BASE_ENV, 1, 1024)
        if code == 1 and not output.strip():
            return absent(pid)  # a denied `ps` exits the same way as a missing process
        return code == 0 and output.strip().startswith(b"Z")
    except OSError, AgentError, subprocess.TimeoutExpired:
        return False


def kill_group(pgid: int) -> bool:
    if os.name == "nt":
        from .verification_win import terminate_named

        try:
            terminate_named(pgid)
        except OSError:
            pass
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass
    return _reaped(pgid)


def kill_worker(pid: int) -> bool:
    """`kill_group` for a worker whose supervisor is alive, so `pid` is still its own. On
    Windows the SDK starts the worker as a plain process with no verifier job, so its tree is
    ended by pid; a dead supervisor's pid may have been reused and goes through `kill_group`."""
    if os.name != "nt":
        return kill_group(pid)
    from ..win_tree import kill_tree
    from .verification_win import terminate_named

    try:
        if not terminate_named(pid):
            kill_tree(pid)
    except OSError:
        pass
    return _reaped(pid)


def _reaped(pgid: int) -> bool:
    deadline = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < deadline:
        if group_dead(pgid):
            return True
        time.sleep(POLL)
    return False


def read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(2 * 1024 * 1024 + 1)
    if not raw or len(raw) > 2 * 1024 * 1024:
        raise SystemExit(2)
    return json.loads(raw)


def command_main() -> None:
    request = read_request()
    if sys.stdin.buffer.readline(4) != b"go\n":
        raise SystemExit(2)
    # The control pipe is not inherited by the shell or its descendants.
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    if os.name == "nt":
        # No exec on Windows: Git Bash runs as this process's child, inside the same job.
        shell = subprocess.Popen(
            [request["shell"], "-c", request["command"]],
            env=request["env"],
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout.fileno(),
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        raise SystemExit(shell.wait())
    os.execve("/bin/sh", ["/bin/sh", "-c", request["command"]], request["env"])  # noqa: S606 — authorized verifier command


def watchdog_main() -> None:
    request = read_request()
    binding = Binding(**request["binding"])
    store = Store(Path(request["root"]), binding)
    reason = "released"
    held: Any = None
    if os.name == "nt":
        from .verification_win import open_named

        # Ready means this watchdog holds the job too, so guardian death alone cannot end it
        # before cleanup is measured, and watchdog death alone leaves the guardian's handle.
        held = open_named(request["pgid"])
        if held is None:
            store.close()
            raise SystemExit(2)
    try:
        sys.stdout.buffer.write(b"ready\n")
        sys.stdout.buffer.flush()
        while not parent_gone():
            if time.monotonic() >= request["deadline"]:
                reason = "timeout"
                break
            time.sleep(POLL)
        cleaned = kill_group(request["pgid"])
        store.write(
            f"cleanup-{request['index']:05d}.json",
            {
                "schema_version": 1,
                "binding": asdict(binding),
                "command_index": request["index"],
                "watchdog_pid": os.getpid(),
                "command_pgid": request["pgid"],
                "cleanup_confirmed": cleaned,
                "reason": reason,
            },
            immutable=True,
        )
    finally:
        store.close()
        if held is not None:
            held.close()
    raise SystemExit(0 if cleaned else 2)


def _unpopulated_gitlinks(worktree: Path, paths: set[bytes]) -> None:
    """Inspect directories without following links or invoking nested Git."""
    if os.name == "nt":
        from .verification_win import gitlink_problem

        if (problem := gitlink_problem(worktree, paths)) is not None:
            raise UnsupportedTree(problem)
        return
    for path in paths:
        parts = os.fsdecode(path).split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise UnsupportedTree("invalid gitlink path")
        fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    break  # Git leaves unpopulated submodule directories absent or empty.
                except OSError:
                    raise UnsupportedTree("gitlink path is not an unpopulated directory") from None
                os.close(fd)
                fd = child
            else:
                with os.scandir(fd) as entries:
                    if next(entries, None) is not None:
                        raise UnsupportedTree("populated gitlink/submodule is unsupported")
        finally:
            os.close(fd)


def open_output_log(store: Store) -> int:
    """Create the run's output log once, refusing anything already at its name."""
    if os.name == "nt":
        from ..win_lock import directory_path

        directory = directory_path(store.root, store.parts, create=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        return os.open(directory / "output.log", flags, 0o600)
    return os.open("output.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=store.fd)


def _hardened_git(
    worktree: Path,
    deadline: float,
    guardian_group: bool = False,
    git: str | None = None,
    argv: list[str] | None = None,
) -> Any:
    """A Git query runner that executes no configured helper program: system Git only, no
    global or system config, no hooks, fsmonitor, external diff, attributes file or filter.
    `git` is the Windows executable a guardian was handed; POSIX always uses `os.defpath`.
    `argv` is a prefix `hardened_argv` already read for this worktree."""
    return _hardened(worktree, deadline, guardian_group, git, argv)[0]


def hardened_argv(worktree: Path, *, timeout: float = 5.0) -> list[str]:
    """The hardened Git prefix for `worktree`, so several observations share one config read."""
    return _hardened(worktree, time.monotonic() + timeout)[1]


def _hardened(
    worktree: Path,
    deadline: float,
    guardian_group: bool = False,
    git: str | None = None,
    argv: list[str] | None = None,
) -> tuple[Any, list[str]]:
    env = {
        **BASE_ENV,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    args: list[str] = list(argv or ())

    # Every query shares the caller's one deadline: with a per-query cap, one slow cold `git` start
    # on a loaded runner could fail the whole observation while the total still had time left.
    def query(tail: list[str], limit: int = 8 * 1024 * 1024) -> tuple[int, bytes]:
        return capture(
            args + tail,
            worktree,
            env,
            deadline - time.monotonic(),
            limit,
            own_group=not guardian_group,
        )

    if argv is not None:
        return query, args
    git = (git if os.name == "nt" else None) or system_git()
    if git is None:
        raise AgentError("system Git is required for verification")
    args += [
        git,
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=" + os.devnull,
    ]
    code, keys = query(
        ["config", "--name-only", "--get-regexp", r"^(filter\..*\.(clean|process|required)|core\.autocrlf)$"]
    )
    if code not in (0, 1):
        raise AgentError("verification could not inspect Git filter configuration")
    names = keys.decode().splitlines()
    for key in names:
        if key.startswith("filter."):
            args += ["-c", key + ("=false" if key.endswith(".required") else "=")]
    if os.name == "nt" and "core.autocrlf" not in names:
        args += ["-c", "core.autocrlf=" + WINDOWS_AUTOCRLF]
    return query, args


def untracked_clean(worktree: Path, *, timeout: float = 5.0, argv: list[str] | None = None) -> bool:
    """True when Git lists no non-ignored untracked file; raises when it cannot tell."""
    query = _hardened_git(worktree, time.monotonic() + timeout, argv=argv)
    code, out = query(["ls-files", "--others", "--exclude-standard", "-z"])
    if code:
        raise AgentError("verification could not list untracked files")
    return out == b""


def observe_tree(
    worktree: Path,
    *,
    timeout: float = 5.0,
    guardian_group: bool = False,
    launch_base: str | None = None,
    git: str | None = None,
    argv: list[str] | None = None,
) -> TreeState:
    """Observe HEAD/index/tracked dirt without executing configured Git helper programs.

    Sparse/skip-worktree and assume-unchanged entries are rejected, because ordinary diff
    can hide their modifications. Untracked files are outside the accepted G07 contract.
    """
    deadline = time.monotonic() + timeout
    query = _hardened_git(worktree, deadline, guardian_group, git, argv)
    code, head = query(["rev-parse", "--verify", "HEAD"], 128)
    if code:
        raise AgentError("verification requires an existing commit")
    code, flags = query(["ls-files", "-v", "--stage", "-z"])
    if code:
        raise AgentError("verification could not inspect index flags")
    links: dict[bytes, tuple[bytes, bytes]] = {}
    for row in flags.split(b"\0"):
        if row and row[2:].startswith(b"160000 "):
            metadata, path = row[2:].split(b"\t", 1)
            mode, oid, stage = metadata.split()
            if stage != b"0":
                raise UnsupportedTree("unmerged gitlink/submodule entry")
            links[path] = (mode, oid)
    if launch_base is None:
        if links:
            raise UnsupportedTree("gitlink/submodule requires an immutable launch base")
    else:
        if not isinstance(launch_base, str) or not re.fullmatch(  # pyright: ignore[reportUnnecessaryIsInstance] -- the base comes from a stored record
            r"[0-9a-f]{40}|[0-9a-f]{64}", launch_base
        ):
            raise UnsupportedTree("invalid immutable launch base")

        def tree_links(ref: str) -> dict[bytes, tuple[bytes, bytes]]:
            code, output = query(["ls-tree", "-r", "-z", ref])
            if code:
                raise UnsupportedTree("immutable gitlink baseline is unavailable")
            result: dict[bytes, tuple[bytes, bytes]] = {}
            for row in output.split(b"\0"):
                if row:
                    metadata, path = row.split(b"\t", 1)
                    mode, kind, oid = metadata.split()
                    if mode == b"160000":
                        if kind != b"commit":
                            raise UnsupportedTree("invalid gitlink object type")
                        result[path] = (mode, oid)
            return result

        current = head.decode().strip()
        if tree_links(launch_base) != links or (current != launch_base and tree_links(current) != links):
            raise UnsupportedTree("gitlink/submodule differs from immutable launch base")
    _unpopulated_gitlinks(worktree, set(links))
    hidden = any(row[:1].islower() or row.startswith(b"S ") for row in flags.split(b"\0") if row)
    tracked, _ = query(
        ["diff", "--quiet", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--exit-code", "--"]
    )
    index, _ = query(
        [
            "diff",
            "--cached",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--exit-code",
            "HEAD",
            "--",
        ]
    )
    if tracked not in (0, 1) or index not in (0, 1):
        raise AgentError("verification Git observation failed")
    _unpopulated_gitlinks(worktree, set(links))
    return TreeState(head.decode().strip(), tracked == 0 and not hidden, index == 0 and not hidden)
