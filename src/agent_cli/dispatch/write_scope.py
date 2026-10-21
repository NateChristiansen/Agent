"""Declare durable write scope and refuse overlapping writers.

Absent `writes` means unknown scope and blocks all other writers in that repo. `writes: []` is a
read-only review: it holds no paths and may probe its own checkout only if it restores every change
and leaves no commits. A nonempty list holds those paths. `no_checks` changes verification, never
scope. Resume may narrow, never widen; record each narrowing with its attempt.

Active records and in-progress resume reservations hold claims under `.agent/dispatch-board/` in
main. `board_lock` serializes overlap checks and refuses conflicting launch or resume.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .. import repos as repos_mod
from .. import win_hold
from ..core import AgentError, agent_path, iso, now
from ..records.narrow import as_dict, as_int, dict_list, str_list
from .worktrees import git_commits, git_files

READ_ONLY = "read-only"
ACTIVE = ("queued", "running", "question")

Holder = tuple[str, str, list[str] | None]  # (name, repo, paths); None paths may hold any path


def canonical(path: str) -> str:
    """A writable path in one spelling: relative, `/`-separated, no `.` or `..` segments, no
    trailing slash. Refused otherwise (`src/x/` and `src/./x/` passed as two)."""
    raw = str(path).strip()
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not raw or raw.startswith("/") or "\\" in raw or ".." in parts or not parts:
        raise AgentError(f"writable path {path!r} must be relative to the repo, without `..`")
    return "/".join(parts)


def overlaps(a: list[str], b: list[str]) -> bool:
    """Two writable path sets conflict when one path is the other or contains it."""
    for x in a:
        for y in b:
            left, right = canonical(x), canonical(y)
            if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                return True
    return False


def scope(writes: list[str] | None) -> str:
    if writes is None:
        return "unknown"
    return READ_ONLY if not writes else "integration"


def canonical_writes(writes: list[str] | None) -> list[str] | None:
    """Declared paths in canonical form; `[]` (read-only) stays apart from None."""
    return None if writes is None else [canonical(w) for w in writes]


# Directory-wide claims block every other writer below them; prefer the narrowest sufficient paths.
BROAD_DIR_FILES = 20
WHOLE_TREES = ("src", "tests")


def broad_writes(repo_path: Path, writes: list[str] | None) -> list[str]:
    """A warning per declared path that is a directory with more than `BROAD_DIR_FILES` tracked
    files, or `src`/`tests` whole. Advisory: the launch still goes ahead."""
    warnings: list[str] = []
    for path in canonical_writes(writes) or []:
        out = repos_mod.git_or_none(["ls-files", "--", path], repo_path) or ""
        files = [line for line in out.splitlines() if line.strip()]
        if files == [path]:
            continue  # a tracked file
        if len(files) > BROAD_DIR_FILES or path in WHOLE_TREES:
            warnings.append(
                f"writes `{path}` is a directory with {len(files)} tracked files: it blocks every other "
                "writer under it. Name the files instead."
            )
    return warnings


def covered(path: str, held: list[str]) -> bool:
    """Whether `path` lies inside one of `held` (the same path, or below a held directory)."""
    return any(path == h or path.startswith(h.rstrip("/") + "/") for h in held)


def narrow(d: dict[str, Any], writes: list[str], attempt: int) -> None:
    """Narrow `d`'s declared paths for its next attempt, in place, under the caller's board lock and
    record guard. Unknown scope may narrow to any declared list; a declared list only to paths it
    already holds, and never back to unknown."""
    new = [canonical(w) for w in writes]
    old = d.get("writes")
    if old is not None:
        wider = [w for w in new if not covered(w, [str(p) for p in old])]
        if wider:
            raise AgentError(
                f"not resumed: writes may only narrow on resume; {', '.join(wider)} is outside "
                f"{', '.join(old) or 'nothing (read-only)'}. Launch a new dispatch for wider paths."
            )
    if old == new:
        return
    entries = dict_list(d.get("writes_history"))
    entries.append({"attempt": attempt, "from": old, "to": new, "at": iso(now())})
    d["writes_history"] = entries
    d["writes"] = new


def broken_read_only(d: dict[str, Any]) -> str | None:
    """Why a read-only review broke its scope (what it left changed), or None."""
    if d.get("writes") != []:
        return None
    worktree = Path(str(d.get("worktree") or ""))
    if not worktree.exists():
        return None
    since = str(d.get("start_sha") or d["base"])
    commits = git_commits(worktree, since)
    changed = git_files(worktree, since)
    if commits:  # a change committed then reverted is still a durable write (net diff empty)
        changed = [*changed, f"{len(commits)} commit(s) since the review started"]
    if not changed:
        return None
    shown = ", ".join(changed[:10]) + (f" and {len(changed) - 10} more" if len(changed) > 10 else "")
    return (
        f"declared no durable writes (`writes: []`, a read-only review) but left changes: {shown}. "
        "Restore probe mutations before returning, or relaunch with the paths it must change."
    )


def broken_scope(d: dict[str, Any]) -> str | None:
    writes = canonical_writes(d.get("writes"))
    if writes is None:
        return None
    if not writes:
        return broken_read_only(d)
    if not d.get("worktree") or not (worktree := Path(d["worktree"])).exists():
        return None
    since = str(d.get("start_sha") or d["base"])
    changed: set[str] = set()
    # Disable rename detection so both the removed and added paths are checked. NUL delimiters
    # preserve spaces, newlines and names that Git would otherwise quote.
    for args in (
        ["diff", "--no-renames", "--name-only", "-z", since, "HEAD", "--"],
        ["diff", "--no-renames", "--name-only", "-z", "--cached", "HEAD", "--"],
        ["diff", "--no-renames", "--name-only", "-z", "--"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        changed.update(p for p in repos_mod.run_git(args, worktree).split("\0") if p)
    outside = sorted(p for p in changed if not covered(p, writes))
    if not outside:
        return None
    return "write scope verification failed; paths outside declared writes: " + ", ".join(
        repr(p) for p in outside
    )


# ------------------------------------------------------------------ the path board


def board_dir(root: Path) -> Path:
    """`.agent/dispatch-board` in the main checkout; `root`'s own when it is not a git checkout."""
    from ..worktree import main_checkout

    try:
        return agent_path(main_checkout(root), "dispatch-board")
    except AgentError:
        return agent_path(root, "dispatch-board")


@contextmanager
def board_lock(root: Path) -> Generator[None]:
    """Serialize launches and resumes, so each checks a board no other is changing."""
    path = board_dir(root) / "board.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with win_hold.blocking(fd):
            yield
    finally:
        os.close(fd)


def board(root: Path) -> list[Holder]:
    """Who holds which writable paths: every active dispatch record with its declared `writes`
    (None when it declared none), and every resume in progress. A running record whose supervisor
    died still holds its paths until its worker's cleanup is confirmed."""
    from ..core import is_alive
    from .state import load_all

    held: list[Holder] = []
    for d in load_all(root):
        if d.get("state") not in ACTIVE:
            continue
        gone = d.get("state") == "running" and not is_alive(d.get("supervisor_pid"))
        if gone and d.get("worker_cleanup_confirmed") and not d.get("worker_cleanup_pending"):
            continue  # a stale record whose worker is confirmed gone: collect reconciles it
        writes = d.get("writes")
        held.append((str(d["id"]), str(d.get("repo") or ""), None if writes is None else str_list(writes)))
    return held + _reservations(root)


def conflict(held: list[Holder], repo: str, writes: list[str]) -> str | None:
    """Why a writer of `writes` in `repo` cannot start now, or None."""
    for name, other_repo, paths in held:
        if other_repo != repo:
            continue
        if paths is None:
            return f"dispatch {name} declares no writable paths"
        if overlaps(writes, paths):
            return f"writes overlap {name}"
    return None


@contextmanager
def reserve(root: Path, repo: str, writes: list[str] | None) -> Generator[None]:
    """Around a new launch's allocation: take the board lock and refuse declared paths that overlap
    running work. The record the allocation saves is on the board before the lock is let go. A
    launch that declares no paths is not checked; it then holds back every later writer."""
    with board_lock(root):
        if writes and (clash := conflict(board(root), repo, [canonical(w) for w in writes])):
            raise AgentError(f"not launched: {clash}")
        yield


def check_resume(root: Path, d: dict[str, Any], narrow_to: list[str] | None = None) -> Callable[[], None]:
    """Under `board_lock`: refuse a resume whose paths overlap someone else's, and publish a
    reservation for its paths that the board reads until the resume saves its queued record
    (a check during the resume's checkout saw its slot but not its paths). Returns
    the undo a failed resume calls: the reservation goes."""
    if str(d["id"]) in resuming(root):  # one resume at a time: both would share its holdings
        raise AgentError(f"dispatch {d['id']} is already being resumed; wait for that resume to finish")
    if narrow_to is not None:  # narrow refuses a widening and records the change
        narrow(d, narrow_to, int(d["attempt"]) + 1)
    held = [h for h in board(root) if h[0] != str(d["id"])]
    if d.get("writes") and (clash := conflict(held, str(d["repo"]), [canonical(w) for w in d["writes"]])):
        raise AgentError(f"not resumed: {clash}")
    reservation = {"repo": d["repo"], "writes": d.get("writes"), "pid": os.getpid()}
    # One file per publication, so one resume's cleanup never drops another's .
    path = board_dir(root) / "reservations" / f"{d['id']}.{secrets.token_hex(6)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reservation), encoding="utf-8")
    return lambda: path.unlink(missing_ok=True)


def resuming(root: Path) -> set[str]:
    """Dispatch ids with a resume in progress (a live reservation)."""
    return {name for name, _, _ in _reservations(root)}


def _reservations(root: Path) -> list[Holder]:
    """Resumes in progress whose process is alive (`check_resume`)."""
    from ..core import is_alive

    out: list[Holder] = []
    for path in sorted((board_dir(root) / "reservations").glob("*.json")):
        try:
            found = as_dict(json.loads(path.read_text(encoding="utf-8")))
        except OSError, ValueError:
            continue
        if is_alive(as_int(found.get("pid")) or None):
            writes = found.get("writes")
            paths = None if writes is None else str_list(writes)
            out.append((path.name.split(".")[0], str(found.get("repo") or ""), paths))
    return out
