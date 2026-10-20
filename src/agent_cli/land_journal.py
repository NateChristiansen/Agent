"""The `agent land` journal, the flock that serializes landings, and the index lock land owns.

Each landing writes `<main>/.agent/land/<id>.json` and fsyncs it at every state change, so a
crash leaves a record of how far it got. Recovery never trusts the journal alone: it observes the
ref, the index, the files and the lock, and the journal only names what to compare them against.

Land holds `.git/index.lock` itself, from before any file is written until the ref and the index
both name the candidate. Git's own lock protocol then makes every other process that would
write the index (`commit`, `add`, `checkout`, `read-tree`) refuse. The lock file's content is the
journal's marker, which is how recovery tells a lock land left behind from a foreign one.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import win_hold
from .core import AgentError, agent_path
from .records.narrow import as_dict_or_none

STATES = (
    "prepared",
    "index_locked",
    "files_applying",
    "files_applied",
    "ref_moving",
    "ref_moved",
    "index_swapped",
    "done",
    "stalled",
    "aborted",
)
TERMINAL = ("done", "aborted")
MARKER = "agent-land"


def land_dir(main: Path) -> Path:
    return agent_path(main, "land")


def path(main: Path, ident: str) -> Path:
    return land_dir(main) / f"{ident}.json"


def temp_index(main: Path, ident: str) -> Path:
    return land_dir(main) / f"{ident}.index"


def new_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        # Windows cannot open a directory as a file, so the rename is not flushed here; NTFS
        # journals its metadata, which is weaker than an fsync.
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(main: Path, journal: dict[str, Any]) -> None:
    """Replace the journal durably: the file and its directory are fsynced."""
    target = path(main, journal["id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, json.dumps(journal, indent=1).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    _fsync_dir(target.parent)


def advance(main: Path, journal: dict[str, Any], state: str, **extra: Any) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"unknown land state {state}")
    journal.update(state=state, ts=now(), **extra)
    write(main, journal)
    return journal


def read(main: Path, ident: str) -> dict[str, Any]:
    try:
        journal = as_dict_or_none(json.loads(path(main, ident).read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise AgentError(f"no land journal {ident}") from None
    except (OSError, ValueError) as exc:
        raise AgentError(f"land journal {ident} is unreadable ({type(exc).__name__})") from None
    if journal is None or journal.get("state") not in STATES:
        raise AgentError(f"land journal {ident} is not a land journal")
    return journal


def every(main: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(land_dir(main).glob("*.json")):
        try:
            out.append(read(main, p.stem))
        except AgentError:
            continue
    return out


def pending(main: Path) -> list[dict[str, Any]]:
    return [j for j in every(main) if j.get("state") not in TERMINAL]


LOCK_POLL_S = 5.0


@contextmanager
def flock(main: Path, wait_s: float = 0.0) -> Generator[None]:
    """Allow one landing or recovery at a time; wait_s bounds queued acquisition instead of requiring
    manual retries.
    """
    land_dir(main).mkdir(parents=True, exist_ok=True)
    fd = os.open(land_dir(main) / "lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    deadline = time.monotonic() + wait_s
    waited = f" after waiting {wait_s:g} s" if wait_s else ""
    try:
        if win_hold.windows():
            try:
                lock = win_hold.hold(fd, timeout=wait_s)
                lock.__enter__()
            except TimeoutError:
                raise AgentError(f"another landing or recovery holds the land lock{waited}") from None
            try:
                yield
            finally:
                lock.__exit__(None, None, None)
            return
        import fcntl

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AgentError(f"another landing or recovery holds the land lock{waited}") from None
                time.sleep(LOCK_POLL_S)
        yield
    finally:
        os.close(fd)


_GIT_DIRS: ContextVar[dict[Path, Path] | None] = ContextVar("land_git_dirs", default=None)


@contextmanager
def git_dir_scope() -> Generator[None]:
    """Within one landing, ask git for each checkout's git directory once. Nothing outlives it."""
    if _GIT_DIRS.get() is not None:
        yield
        return
    token = _GIT_DIRS.set({})
    try:
        yield
    finally:
        _GIT_DIRS.reset(token)


def git_dir(main: Path) -> Path:
    seen = _GIT_DIRS.get()
    if seen is not None and main in seen:
        return seen[main]
    out = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=main, capture_output=True, text=True, check=False
    )
    if out.returncode:
        raise AgentError(f"cannot find the git directory of {main}")
    found = Path(out.stdout.strip())
    if seen is not None:
        seen[main] = found
    return found


def index_file(main: Path) -> Path:
    return git_dir(main) / "index"


def lock_file(main: Path) -> Path:
    return git_dir(main) / "index.lock"


def marker(journal: dict[str, Any]) -> str:
    return f"{MARKER} {journal['id']} {journal['pid']} {journal['pid_start']}\n"


def take_lock(main: Path, journal: dict[str, Any]) -> None:
    """Create `.git/index.lock` holding this journal's marker, or refuse if anything holds it."""
    try:
        fd = os.open(lock_file(main), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise AgentError(
            "the main checkout's index is locked by another git process (.git/index.lock exists); "
            "nothing was changed"
        ) from None
    try:
        os.write(fd, marker(journal).encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def lock_state(main: Path, journal: dict[str, Any]) -> str:
    """`absent`, `ours` (this journal's marker) or `foreign`."""
    try:
        content = lock_file(main).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "foreign"
    return "ours" if content == marker(journal) else "foreign"


def release(main: Path, journal: dict[str, Any]) -> None:
    """Remove the lock only when it is this journal's own."""
    if lock_state(main, journal) == "ours":
        lock_file(main).unlink()


def started(pid: int) -> str:
    from .procs import process_started

    return process_started(pid)


def alive(journal: dict[str, Any]) -> bool:
    """The journal's process is still running: same pid and same start time."""
    pid = journal.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    now_started = started(pid)
    return bool(now_started) and now_started == journal.get("pid_start")


OP_TERMINAL = ("done", "failed")


def op_path(main: Path, op_id: str) -> Path:
    return land_dir(main) / f"op-{op_id}.json"


def op_update(main: Path, op_id: str | None, **fields: Any) -> None:
    """Record landing progress from request through queued, waiting-for-lock, verifying, landing and
    terminal stages, including before a journal exists. Best effort: progress recording must never
    fail the landing.
    """
    if not op_id:
        return
    path = op_path(main, op_id)
    try:
        land_dir(main).mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        return
    try:
        # The detached parent and its child both write this record: serialize the read-modify-
        # write, and never let a later note (the parent's pid) move a finished operation back
        # (Codex's round-2 review saw `failed` reverted to `queued`).
        with win_hold.blocking(fd):
            current = as_dict_or_none(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None
            if current is not None and current.get("state") in OP_TERMINAL:
                fields = {k: v for k, v in fields.items() if k not in ("state", "why")}
            op: dict[str, Any] = {**(current or {"id": op_id}), **fields, "updated": now()}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(op, indent=1) + "\n", encoding="utf-8")
            tmp.replace(path)
    except OSError, ValueError:
        return
    finally:
        os.close(fd)


def ops_recent(main: Path, n: int = 5) -> list[dict[str, Any]]:
    """The newest `n` landing operations, newest first."""
    out: list[dict[str, Any]] = []
    for p in sorted(land_dir(main).glob("op-*.json"), reverse=True)[:n]:
        try:
            if (op := as_dict_or_none(json.loads(p.read_text(encoding="utf-8")))) is not None:
                out.append(op)
        except OSError, ValueError:
            continue
    return out
