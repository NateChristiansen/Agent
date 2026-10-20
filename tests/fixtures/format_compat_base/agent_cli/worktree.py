# ruff: noqa
from __future__ import annotations
import shutil
import subprocess
from pathlib import Path
from typing import Any
from . import git_discovery
from . import notes as notes_mod
from . import repos as repos_mod
from .core import AgentError, ExternalFailure, agent_dir_name, agent_root

BRANCH_PREFIX = "agent/"
STATE_DIR = agent_dir_name()
WORKTREES_DIRNAME = "Agent-worktrees"


def _git(args: list[str], cwd: Path) -> str:
    return repos_mod.run_git(args, cwd)


def main_checkout(root: Path) -> Path:
    found = git_discovery.common_dir(root)
    if found is not None:
        return found.parent
    common = _git(["rev-parse", "--git-common-dir"], root).strip()
    path = Path(common)
    if not path.is_absolute():
        path = (root / path).resolve()
    return path.parent


def is_worktree(root: Path) -> bool:
    try:
        return main_checkout(root).resolve() != root.resolve()
    except AgentError, ExternalFailure, OSError:
        return False


def state_link_ok(root: Path) -> tuple[bool, str]:
    state = agent_root(root)
    try:
        main = main_checkout(root)
    except (AgentError, ExternalFailure, OSError) as exc:
        return (True, f"not a git checkout ({exc}); nothing to check")
    if main.resolve() == root.resolve():
        if state.is_symlink():
            return (False, f"{STATE_DIR}/ in the main checkout is a symlink; it owns the real directory")
        return (True, f"main checkout; {STATE_DIR}/ is the shared state")
    from . import links

    want = agent_root(main).resolve()
    if not state.exists() and (not links.is_link(state)):
        return (False, f"no {STATE_DIR}/; run `agent worktree link` so this session shares {want}")
    if not links.is_link(state):
        return (
            False,
            f"{STATE_DIR}/ is a real directory, so this worktree coordinates with itself: peer list, leases and dispatch ids are all split from {want}. Move it aside and run `agent worktree link`",
        )
    if state.resolve() != want:
        return (False, f"{STATE_DIR}/ points at {state.resolve()}, not the main checkout's {want}")
    return (True, f"shares {want}")


def link_state(root: Path) -> dict[str, Any]:
    main = main_checkout(root)
    if main.resolve() == root.resolve():
        raise AgentError(f"{root} is the main checkout; it owns {STATE_DIR}/ rather than linking to it")
    target = agent_root(main)
    if not target.is_dir():
        raise AgentError(f"{target} does not exist; the main checkout has no state to share")
    from . import links

    state = agent_root(root)
    if links.is_link(state):
        if state.resolve() == target.resolve():
            return {"linked": False, "path": str(state), "target": str(target)}
        state.unlink() if state.is_symlink() else state.rmdir()
    elif state.exists():
        raise AgentError(
            f"{state} is a real directory. Nothing here deletes coordination state: move it aside yourself, then run this again"
        )
    kind = links.make(state, target, is_dir=True)
    return {"linked": True, "path": str(state), "target": str(target), "kind": kind}


def worktrees_dir(main: Path) -> Path:
    return main.parent / WORKTREES_DIRNAME


def worktree_path(main: Path, name: str) -> Path:
    return worktrees_dir(main) / name


def _component(name: str) -> str:
    slug = name.strip()
    if not slug or slug in (".", "..") or any((c in slug for c in "/\\ :\x00")):
        raise AgentError(
            f"worktree name {name!r} must be one directory name: no slashes, spaces, `.` or `..`"
        )
    return slug


def create(root: Path, name: str, *, base: str = "main", sync: bool = True) -> dict[str, Any]:
    slug = _component(name)
    main = main_checkout(root)
    path = worktree_path(main, slug)
    branch = f"{BRANCH_PREFIX}{slug}"
    steps: list[str] = []
    existing = {Path(w["path"]).resolve(): w for w in repos_mod.worktrees(main)}
    if path.resolve() in existing:
        steps.append(f"worktree already at {path}")
    elif path.exists():
        raise AgentError(f"{path} exists and is not a worktree; remove it or pick another name")
    else:
        head = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], main)
        if head and any((w.get("branch") == branch for w in existing.values())):
            raise AgentError(f"branch {branch} is checked out in another worktree")
        path.parent.mkdir(parents=True, exist_ok=True)
        from .dispatch.allocation import git_admin, git_admin_short

        with git_admin(main, git_admin_short(root, main)):
            if head:
                _git(["worktree", "add", str(path), branch], main)
            else:
                _git(["worktree", "add", "-b", branch, str(path), base], main)
        steps.append(f"worktree added at {path} on {branch}")
    linked = link_state(path)
    steps.append(
        f"{STATE_DIR}/ -> {linked['target']}" if linked["linked"] else f"{STATE_DIR}/ already shared"
    )
    notes_root = notes_mod.read_notes_root(root)
    if notes_root:
        notes_mod.link(path, notes_root)
        steps.append(f"private set linked from {notes_root}")
    else:
        steps.append("no notes repo recorded; the private set stays as plain files")
    if sync:
        steps.append(_sync(path))
    steps.append(_render(path))
    return {"path": str(path), "branch": branch, "steps": steps}


def _render(path: Path) -> str:
    from . import status as status_mod

    try:
        status_mod.render(path)
    except Exception as exc:
        return f"generated files not written ({exc}); the worktree's own `status_render` tool writes them"
    return "STATUS.md, Dashboard.md, Index.md and bases/ written"


def _sync(path: Path) -> str:
    uv = shutil.which("uv")
    if not uv:
        return "uv is not on PATH; run `uv sync` in the worktree yourself"
    try:
        subprocess.run(
            [uv, "sync", "--quiet"], cwd=path, capture_output=True, text=True, check=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"uv sync failed ({exc}); run it in the worktree yourself"
    return ".venv synced"


def list_all(root: Path) -> list[dict[str, Any]]:
    main = main_checkout(root)
    out: list[dict[str, Any]] = []
    for entry in repos_mod.worktrees(main):
        path = Path(entry["path"])
        if path.resolve() == main.resolve():
            continue
        branch = str(entry.get("branch") or "")
        ok, detail = state_link_ok(path)
        out.append(
            {
                "path": str(path),
                "branch": branch,
                "manager": branch.startswith(BRANCH_PREFIX),
                "state_ok": ok,
                "state": detail,
            }
        )
    return out


def remove(root: Path, name: str, *, force: bool = False) -> dict[str, Any]:
    slug = _component(name)
    main = main_checkout(root)
    known = {Path(w["path"]).resolve() for w in repos_mod.worktrees(main)}
    path = worktree_path(main, slug)
    if path.resolve() not in known:
        raise AgentError(f"{path} is not a worktree of this repository")
    dirty = _git(["status", "--porcelain"], path).strip()
    if dirty and (not force):
        raise AgentError(f"{path} has uncommitted changes; commit them or pass --force")
    unlinked = unlink_links(path)
    from .dispatch.allocation import git_admin, git_admin_short

    with git_admin(main, git_admin_short(root, main)):
        _git(["worktree", "remove", "--force", str(path)], main)
    return {"path": str(path), "removed": True, "branch_kept": True, "unlinked": unlinked}


def unlink_links(path: Path) -> list[str]:
    from . import links

    removed: list[str] = []
    stack = [path]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            if links.is_link(entry):
                try:
                    entry.unlink() if entry.is_symlink() else entry.rmdir()
                except OSError as exc:
                    raise AgentError(f"could not unlink {entry} before removing {path}: {exc}") from exc
                removed.append(str(entry))
            elif entry.is_dir():
                stack.append(entry)
    return removed
