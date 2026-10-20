"""Isolate each manager's code and venv in a worktree while sharing coordination state. Leases
serialize edits but cannot contain a broken import; separate checkouts keep one session's partial
edit from disabling every client's hooks and tools.

Link each worktree's ignored .agent to main so sessions, leases, peers, dispatch records and live
snapshots remain one store. Fragmentation can hide peers and permit identity collisions without an
error. The launcher derives AGENT_ROOT from its checkout and runs that code against shared state.
"""

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
STATE_DIR = agent_dir_name()  # for messages; the path itself is `agent_root(...)`
WORKTREES_DIRNAME = "Agent-worktrees"


def _git(args: list[str], cwd: Path) -> str:
    return repos_mod.run_git(args, cwd)


def main_checkout(root: Path) -> Path:
    """The checkout that owns the repository, whether `root` is it or a worktree of it.

    `--git-common-dir` is the shared `.git` for every worktree, so its parent is the main
    checkout. This is what a worktree's `.agent` symlink must point at, and guessing it from
    the directory name would break the moment someone renames a directory.
    """
    found = git_discovery.common_dir(root)  # git's own discovery without starting git
    if found is not None:
        return found.parent
    common = _git(["rev-parse", "--git-common-dir"], root).strip()
    path = Path(common)
    if not path.is_absolute():
        path = (root / path).resolve()
    return path.parent


def is_worktree(root: Path) -> bool:
    """True when `root` is a linked worktree rather than the main checkout."""
    try:
        return main_checkout(root).resolve() != root.resolve()
    except AgentError, ExternalFailure, OSError:
        return False


def state_link_ok(root: Path) -> tuple[bool, str]:
    """Whether this checkout's `.agent` reaches the main checkout's. `(ok, detail)`.

    The main checkout owns a real directory. A worktree must carry a symlink to it; a real
    directory there means the session is coordinating with itself — `peer list` shows one
    session, leases arbitrate nothing, and two dispatches can take the same id.
    """
    state = agent_root(root)
    try:
        main = main_checkout(root)
    except (AgentError, ExternalFailure, OSError) as exc:
        return True, f"not a git checkout ({exc}); nothing to check"
    if main.resolve() == root.resolve():
        if state.is_symlink():
            return False, f"{STATE_DIR}/ in the main checkout is a symlink; it owns the real directory"
        return True, f"main checkout; {STATE_DIR}/ is the shared state"
    from . import links

    want = agent_root(main).resolve()
    if not state.exists() and not links.is_link(state):
        return False, f"no {STATE_DIR}/; run `agent worktree link` so this session shares {want}"
    if not links.is_link(state):  # a junction counts: Windows without symlink rights makes one
        return (
            False,
            f"{STATE_DIR}/ is a real directory, so this worktree coordinates with itself: "
            f"peer list, leases and dispatch ids are all split from {want}. "
            f"Move it aside and run `agent worktree link`",
        )
    if state.resolve() != want:
        return False, f"{STATE_DIR}/ points at {state.resolve()}, not the main checkout's {want}"
    return True, f"shares {want}"


def link_state(root: Path) -> dict[str, Any]:
    """Point this worktree's `.agent` at the main checkout's. Idempotent."""
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
        state.unlink() if state.is_symlink() else state.rmdir()  # a junction is removed as a dir
    elif state.exists():
        raise AgentError(
            f"{state} is a real directory. Nothing here deletes coordination state: move it "
            f"aside yourself, then run this again"
        )
    # A symlink where allowed; a junction on a Windows account without symlink rights, which
    # failed `worktree_new` with WinError 1314.
    kind = links.make(state, target, is_dir=True)
    return {"linked": True, "path": str(state), "target": str(target), "kind": kind}


def worktrees_dir(main: Path) -> Path:
    """The one parent every manager worktree lives under: `../Agent-worktrees/`.

    Outside the vault, because every CLAUDE.md and AGENTS.md from cwd up to root loads into a
    session — a checkout nested inside the vault would inherit the manager context twice. One
    fixed directory, so one stable parent grant covers every worktree: `../<main>-wt-<name>`
    per worktree sat outside both clients' grants, so Claude prompted on each edit there and
    Codex refused the write. (Claude's `additionalDirectories` takes literal paths; Codex
    profiles also accept globs, but one parent keeps the two grants the same.) Separate from `../repos/`, so product-repo guards never treat it as product.
    Setup grants it on both clients (`worktree_grants`).
    """
    return main.parent / WORKTREES_DIRNAME


def worktree_path(main: Path, name: str) -> Path:
    return worktrees_dir(main) / name


def _component(name: str) -> str:
    """`name` as one directory under the parent, or AgentError. The generated ask rules match
    `<parent>/*/<path>`, one level: `team/one` sat a level deeper, outside every rule, and `..`
    leaves the parent. Checked before any mkdir or git call, so a refusal leaves nothing."""
    slug = name.strip()
    if not slug or slug in (".", "..") or any(c in slug for c in "/\\ :\0"):
        raise AgentError(
            f"worktree name {name!r} must be one directory name: no slashes, spaces, `.` or `..`"
        )
    return slug


def create(root: Path, name: str, *, base: str = "main", sync: bool = True) -> dict[str, Any]:
    """Add a manager worktree on `agent/<name>`, share the state, link the private set.

    Every step is separately idempotent, so re-running after a failure finishes the job rather
    than starting a second broken one.
    """
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
        # Use git_or_none for an absent ref: rev-parse --verify --quiet exits 1 normally, while
        # _git treats it as failure.
        head = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], main)
        if head and any(w.get("branch") == branch for w in existing.values()):
            raise AgentError(f"branch {branch} is checked out in another worktree")
        path.parent.mkdir(parents=True, exist_ok=True)
        from .dispatch.allocation import git_admin, git_admin_short

        # Guarded in the main checkout's `.agent`, the one every manager worktree links to.
        with git_admin(main, git_admin_short(root, main)):
            if head:
                # The branch outlives its worktree — `remove` keeps it on purpose, because the work
                # on it is the point. Recreating checks that branch out; passing `base` here instead
                # tried to check out `main`, which the main checkout already holds, and git refused.
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
    """Write the generated files this worktree has none of.

    `Dashboard.md`, `Index.md` and `bases/` are gitignored in every clone
    (see ADR-0016), so a fresh worktree starts without them and `doctor` opens with four failures
    before the session has done anything. Found by running `doctor` inside the first worktree
    rather than by reasoning about it.
    """
    from . import status as status_mod

    try:
        status_mod.render(path)
    except Exception as exc:  # noqa: BLE001 — a worktree without views is usable, just noisy
        return f"generated files not written ({exc}); the worktree's own `status_render` tool writes them"
    return "Dashboard.md, Index.md and bases/ written"


def _sync(path: Path) -> str:
    """`uv sync` in the worktree. Its own `.venv`, because `bin/agent` prefers the local one and
    a worktree on another branch may not want the main checkout's dependency set."""
    uv = shutil.which("uv")
    if not uv:
        return "uv is not on PATH; run `uv sync` in the worktree yourself"
    try:
        subprocess.run(  # noqa: S603 — fixed argv, resolved binary
            [uv, "sync", "--quiet"], cwd=path, capture_output=True, text=True, check=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"uv sync failed ({exc}); run it in the worktree yourself"
    return ".venv synced"


def list_all(root: Path) -> list[dict[str, Any]]:
    """Every manager worktree of this repository, with whether its state is shared."""
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
    """Remove a manager worktree. Refuses a dirty one unless forced, and never touches the
    branch: the work on it is the point, and a worktree is cheap to recreate."""
    slug = _component(name)
    main = main_checkout(root)
    known = {Path(w["path"]).resolve() for w in repos_mod.worktrees(main)}
    path = worktree_path(main, slug)
    if path.resolve() not in known:
        raise AgentError(f"{path} is not a worktree of this repository")
    dirty = _git(["status", "--porcelain"], path).strip()
    if dirty and not force:
        raise AgentError(f"{path} has uncommitted changes; commit them or pass --force")
    unlinked = unlink_links(path)
    from .dispatch.allocation import git_admin, git_admin_short

    # The dirty check above is the gate. Removing a link that stood over tracked files leaves
    # git seeing them deleted, so git's own check would refuse a worktree that was clean.
    with git_admin(main, git_admin_short(root, main)):
        _git(["worktree", "remove", "--force", str(path)], main)
    return {"path": str(path), "removed": True, "branch_kept": True, "unlinked": unlinked}


def unlink_links(path: Path) -> list[str]:
    """Remove every symlink and junction inside a worktree, never following one. `.agent`
    points at the shared state and the private set at the notes repo; on Windows `git
    worktree remove --force` recursed through the `.agent` junction and deleted live dispatch
    records. A link that cannot be removed stops the removal before git runs."""
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
