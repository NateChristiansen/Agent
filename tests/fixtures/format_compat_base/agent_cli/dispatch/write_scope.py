# ruff: noqa
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
Holder = tuple[str, str, list[str] | None]


def canonical(path: str) -> str:
    raw = str(path).strip()
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not raw or raw.startswith("/") or "\\" in raw or (".." in parts) or (not parts):
        raise AgentError(f"writable path {path!r} must be relative to the repo, without `..`")
    return "/".join(parts)


def overlaps(a: list[str], b: list[str]) -> bool:
    for x in a:
        for y in b:
            left, right = (canonical(x), canonical(y))
            if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                return True
    return False


def scope(writes: list[str] | None) -> str:
    if writes is None:
        return "unknown"
    return READ_ONLY if not writes else "integration"


def canonical_writes(writes: list[str] | None) -> list[str] | None:
    return None if writes is None else [canonical(w) for w in writes]


BROAD_DIR_FILES = 20
WHOLE_TREES = ("src", "tests")


def broad_writes(repo_path: Path, writes: list[str] | None) -> list[str]:
    warnings: list[str] = []
    for path in canonical_writes(writes) or []:
        out = repos_mod.git_or_none(["ls-files", "--", path], repo_path) or ""
        files = [line for line in out.splitlines() if line.strip()]
        if files == [path]:
            continue
        if len(files) > BROAD_DIR_FILES or path in WHOLE_TREES:
            warnings.append(
                f"writes `{path}` is a directory with {len(files)} tracked files: it blocks every other writer under it. Name the files instead."
            )
    return warnings


def covered(path: str, held: list[str]) -> bool:
    return any((path == h or path.startswith(h.rstrip("/") + "/") for h in held))


def narrow(d: dict[str, Any], writes: list[str], attempt: int) -> None:
    new = [canonical(w) for w in writes]
    old = d.get("writes")
    if old is not None:
        wider = [w for w in new if not covered(w, [str(p) for p in old])]
        if wider:
            raise AgentError(
                f"not resumed: writes may only narrow on resume; {', '.join(wider)} is outside {', '.join(old) or 'nothing (read-only)'}. Launch a new dispatch for wider paths."
            )
    if old == new:
        return
    entries = dict_list(d.get("writes_history"))
    entries.append({"attempt": attempt, "from": old, "to": new, "at": iso(now())})
    d["writes_history"] = entries
    d["writes"] = new


def broken_read_only(d: dict[str, Any]) -> str | None:
    if d.get("writes") != []:
        return None
    worktree = Path(str(d.get("worktree") or ""))
    if not worktree.exists():
        return None
    since = str(d.get("start_sha") or d["base"])
    commits = git_commits(worktree, since)
    changed = git_files(worktree, since)
    if commits:
        changed = [*changed, f"{len(commits)} commit(s) since the review started"]
    if not changed:
        return None
    shown = ", ".join(changed[:10]) + (f" and {len(changed) - 10} more" if len(changed) > 10 else "")
    return f"declared no durable writes (`writes: []`, a read-only review) but left changes: {shown}. Restore probe mutations before returning, or relaunch with the paths it must change."


def board_dir(root: Path) -> Path:
    from ..worktree import main_checkout

    try:
        return agent_path(main_checkout(root), "dispatch-board")
    except AgentError:
        return agent_path(root, "dispatch-board")


@contextmanager
def board_lock(root: Path) -> Generator[None]:
    path = board_dir(root) / "board.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 384)
    try:
        with win_hold.blocking(fd):
            yield
    finally:
        os.close(fd)


def board(root: Path) -> list[Holder]:
    from ..core import is_alive
    from .state import load_all

    held: list[Holder] = []
    for d in load_all(root):
        if d.get("state") not in ACTIVE:
            continue
        gone = d.get("state") == "running" and (not is_alive(d.get("supervisor_pid")))
        if gone and d.get("worker_cleanup_confirmed") and (not d.get("worker_cleanup_pending")):
            continue
        writes = d.get("writes")
        held.append((str(d["id"]), str(d.get("repo") or ""), None if writes is None else str_list(writes)))
    return held + _reservations(root)


def conflict(held: list[Holder], repo: str, writes: list[str]) -> str | None:
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
    with board_lock(root):
        if writes and (clash := conflict(board(root), repo, [canonical(w) for w in writes])):
            raise AgentError(f"not launched: {clash}")
        yield


def check_resume(root: Path, d: dict[str, Any], narrow_to: list[str] | None = None) -> Callable[[], None]:
    if str(d["id"]) in resuming(root):
        raise AgentError(f"dispatch {d['id']} is already being resumed; wait for that resume to finish")
    if narrow_to is not None:
        narrow(d, narrow_to, int(d["attempt"]) + 1)
    held = [h for h in board(root) if h[0] != str(d["id"])]
    if d.get("writes") and (clash := conflict(held, str(d["repo"]), [canonical(w) for w in d["writes"]])):
        raise AgentError(f"not resumed: {clash}")
    reservation = {"repo": d["repo"], "writes": d.get("writes"), "pid": os.getpid()}
    path = board_dir(root) / "reservations" / f"{d['id']}.{secrets.token_hex(6)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reservation), encoding="utf-8")
    return lambda: path.unlink(missing_ok=True)


def resuming(root: Path) -> set[str]:
    return {name for name, _, _ in _reservations(root)}


def _reservations(root: Path) -> list[Holder]:
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
