"""Isolate each manager's code and venv in a worktree while sharing coordination state. Leases
serialize edits but cannot contain a broken import; separate checkouts keep one session's partial
edit from disabling every client's hooks and tools.

Link each worktree's ignored .agent to main so sessions, leases, peers, dispatch records and live
snapshots remain one store. Fragmentation can hide peers and permit identity collisions without an
error. The launcher derives AGENT_ROOT from its checkout and runs that code against shared state.
"""

from __future__ import annotations

import functools
import os
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


def is_main_checkout_of(root: Path, main: Path) -> bool:
    """Whether `main` is this harness's own checkout; False when the harness root is not in Git."""
    try:
        return main_checkout(root).resolve() == main.resolve()
    except AgentError, ExternalFailure, OSError:
        return False


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


def create(
    root: Path, name: str, *, base: str = "main", sync: bool = True, expect_head: str | None = None
) -> dict[str, Any]:
    """Add a manager worktree on `agent/<name>`, share the state, link the private set.

    Every step is separately idempotent, so re-running after a failure finishes the job rather
    than starting a second broken one. An existing branch is checked out as it is: when it is not
    at `base` the result says so in `branch_differs`, and a `base` other than main refuses.
    `expect_head` makes the add refuse unless the branch is still at that commit under the
    git-admin guard.
    """
    slug = _component(name)
    main = main_checkout(root)
    path = worktree_path(main, slug)
    branch = f"{BRANCH_PREFIX}{slug}"
    steps: list[str] = []
    differs: dict[str, str] | None = None

    existing = {Path(w["path"]).resolve(): w for w in repos_mod.worktrees(main)}
    seen = registration(main, path)
    if seen["state"] == "present":
        current = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], path)
        differs = _base_differs(main, branch, (current or "").strip(), base)
        steps.append(f"worktree already at {path}")
    elif seen.get("admin"):
        raise AgentError(
            f"{path} is registered but {seen['state']}; preview `harness` action `worktree_restore` "
            "or `worktree_reconcile` for it. Nothing was added"
        )
    elif seen["state"] not in ("missing", "root-unavailable"):
        raise AgentError(f"{path} exists and is not a worktree; remove it or pick another name")
    else:
        # Use git_or_none for an absent ref: rev-parse --verify --quiet exits 1 normally, while
        # _git treats it as failure.
        head = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], main)
        if head and any(w.get("branch") == branch for w in existing.values()):
            raise AgentError(f"branch {branch} is checked out in another worktree")
        if head and expect_head is None:
            differs = _base_differs(main, branch, head.strip(), base)
        path.parent.mkdir(parents=True, exist_ok=True)
        from .dispatch.allocation import git_admin, git_admin_short

        # Guarded in the main checkout's `.agent`, the one every manager worktree links to.
        with git_admin(main, git_admin_short(root, main)):
            if expect_head is not None:
                now = repos_mod.git_or_none(
                    ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], main
                )
                if (now or "").strip() != expect_head or os.path.lexists(path):
                    raise AgentError(f"{branch} or {path} changed before the restore; nothing was added")
            if head:
                # The branch outlives its worktree — `remove` keeps it on purpose, because the work
                # on it is the point. Recreating checks that branch out; passing `base` here instead
                # tried to check out `main`, which the main checkout already holds, and git refused.
                _git(["worktree", "add", str(path), branch], main)
            else:
                _git(["worktree", "add", "-b", branch, str(path), base], main)
        steps.append(f"worktree added at {path} on {branch}")
    if differs:
        steps.append(
            f"{branch} already existed at {differs['branch_tip'][:12]}, not at {base}; "
            "checked out as it is, not reset"
        )

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
    return {
        "path": str(path),
        "branch": branch,
        "steps": steps,
        **({"branch_differs": differs} if differs else {}),
    }


def _base_differs(main: Path, branch: str, tip: str, base: str) -> dict[str, str] | None:
    """How an existing checkout or branch at `tip` differs from `base`; refuses when the caller
    named a base other than the default, since nothing here resets a branch."""
    want = (
        repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], main) or ""
    ).strip()
    if want == tip:
        return None
    if base != "main":
        raise AgentError(
            f"{branch} already exists at {tip[:12] or '?'}, not at base {base}; nothing was added. "
            "Omit base to use the branch as it is, or pick another name"
        )
    return {"branch_tip": tip, "base": base, "base_commit": want}


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
    """Remove a manager worktree through tidy's removal policy, and never touch the branch: the
    work on it is the point, and a worktree is cheap to recreate. `force` waives only tracked
    edits; untracked or ignored durable files, activity, locks, a HEAD off the branch tip and any
    dispatch record on the path that is active or has unconfirmed cleanup always hold."""
    slug = _component(name)
    main = main_checkout(root)
    path = worktree_path(main, slug)
    seen = registration(main, path)
    if not seen.get("admin"):
        raise AgentError(f"{path} is not a worktree of this repository")
    from . import tidy
    from .dispatch.allocation import git_admin, git_admin_short
    from .dispatch.state import load_all

    branch = f"{BRANCH_PREFIX}{slug}"

    def holds(expect: dict[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
        records = [d for d in load_all(root) if d.get("worktree") and _same(d["worktree"], path)]
        return tidy.removal_holds(
            root,
            main,
            path,
            branch,
            None,
            records,
            collected={},
            force=force,
            expect=expect,
            require_resolved=False,
        )

    reasons, decided = holds(None)
    if reasons:
        raise AgentError(f"{path} kept: " + "; ".join(reasons))
    # The checks above are the gate, repeated under the guard. Removing a link that stood over
    # tracked files leaves git seeing them deleted, so git's own check would refuse a clean tree.
    with git_admin(main, git_admin_short(root, main)):
        if reasons := holds(decided)[0]:
            raise AgentError(f"{path} kept: " + "; ".join(reasons))
        unlinked = unlink_links(path)
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


LOST_FILES_WARNING = (
    "unstaged and untracked files of the lost checkout are not recoverable from Git; none were recreated"
)


def _same(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _path_state(path: Path) -> str:
    """`present`, `missing` (its parent is readable and it is not there), `root-unavailable` (a
    parent is gone: a moved or unmounted root, not proof of deletion) or `inaccessible`."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        try:
            os.stat(path.parent)
        except FileNotFoundError:
            return "root-unavailable"
        except OSError:
            return "inaccessible"
        return "missing"
    except OSError:
        return "inaccessible"
    return "present"


@functools.lru_cache(maxsize=64)
def _common_dir(main: Path) -> Path:
    common = git_discovery.common_dir(main) or Path(_git(["rev-parse", "--git-common-dir"], main).strip())
    return common if common.is_absolute() else (main / common).resolve()


def _admin_dirs(main: Path) -> list[Path]:
    admin = _common_dir(main) / "worktrees"
    return sorted(p for p in admin.iterdir() if p.is_dir()) if admin.is_dir() else []


def _admin_head(main: Path, admin: Path) -> tuple[str, str, str]:
    """(commit, branch, source) of the admin record's HEAD; a detached HEAD has no branch. A
    symbolic HEAD only names a branch, which can move after the checkout is lost, so its commit
    comes from the worktree's own HEAD reflog (`reflog`); `ref` means only the branch's current
    tip was available, and that is no proof of where the checkout was."""
    raw = (admin / "HEAD").read_text(encoding="utf-8").strip()
    if not raw.startswith("ref: "):
        sha = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{raw}^{{commit}}"], main)
        return (sha or "").strip(), "", "detached"
    branch = raw.removeprefix("ref: refs/heads/")
    log = admin / "logs" / "HEAD"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
    last = lines[-1].split() if lines else []
    if len(last) > 1:
        sha = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{last[1]}^{{commit}}"], main)
        if sha:
            return sha.strip(), branch, "reflog"
    sha = repos_mod.git_or_none(
        ["rev-parse", "--verify", "--quiet", f"{raw.removeprefix('ref: ')}^{{commit}}"], main
    )
    return (sha or "").strip(), branch, "ref"


def _index_matches(main: Path, index: Path, commit: str) -> bool:
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    probe = subprocess.run(  # noqa: S603 — fixed argv
        ["git", "diff-index", "--cached", "--quiet", commit, "--"],
        cwd=main,
        env=env,
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def registration(main: Path, path: Path) -> dict[str, Any]:
    """What Git and the filesystem each say about one worktree path, read only. Git's admin record
    must name the path and the path's `.git` must name that record back; anything else is an
    `occupied` path, not this worktree. A missing path's HEAD and index count as preserved only
    when a ref contains that HEAD and the index equals HEAD's tree or a `refs/recovery/` snapshot."""
    info: dict[str, Any] = {"path": str(path), "state": _path_state(path), "admin": None}
    admin = next(
        (
            a
            for a in _admin_dirs(main)
            if (a / "gitdir").is_file()
            and _same(Path((a / "gitdir").read_text(encoding="utf-8").strip()).parent, path)
        ),
        None,
    )
    if info["state"] == "present":
        dot_git = path / ".git"
        named = ""
        if dot_git.is_file() and not dot_git.is_symlink():
            named = dot_git.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
        if admin is None or not named or not _same(named, admin):
            info["state"] = "occupied"
    if admin is None:
        return info
    info.update(admin=str(admin), locked=(admin / "locked").exists())
    if info["state"] == "present":
        return info
    info["head"], info["branch"], info["head_source"] = _admin_head(main, admin)
    head = info["head"]
    contains = (
        repos_mod.git_or_none(["for-each-ref", "--count=1", "--contains", head], main) if head else None
    )
    info["head_preserved"] = bool(contains and contains.strip())
    index = admin / "index"
    if index.is_file() and head and not _index_matches(main, index, head):
        refs = repos_mod.git_or_none(["for-each-ref", "--format=%(refname)", "refs/recovery/"], main) or ""
        index_ref = next((r for r in refs.split() if _index_matches(main, index, r)), "")
        info["index_preserved"] = bool(index_ref)
        info["staged_ref"] = index_ref
    else:
        info["index_preserved"] = bool(head)
    return info


def reconcile_holds(info: dict[str, Any]) -> list[str]:
    if not info.get("admin"):
        return ["Git has no worktree registered at this path"]
    reasons: list[str] = []
    if info["state"] != "missing":
        reasons.append(f"path is {info['state']}; only a proven-missing path is reconciled")
    if info.get("locked"):
        reasons.append("the registration is locked (`git worktree lock`)")
    if info["state"] == "missing" and not info.get("head_preserved"):
        reasons.append(f"HEAD {info.get('head') or '(unreadable)'} is on no ref; pin it before reconciling")
    if info["state"] == "missing" and not info.get("index_preserved"):
        reasons.append(
            "its index differs from HEAD and no refs/recovery/ snapshot holds it; preserve it first"
        )
    return reasons


def forget_missing(root: Path, main: Path, path: Path, *, apply: bool = False) -> dict[str, Any]:
    """Drop one missing path's Git registration, never a global prune: the admin record is
    removed only while the path is still missing and its HEAD and index stay preserved."""
    info = registration(main, path)
    holds = reconcile_holds(info)
    if holds or not apply:
        return {**info, "holds": holds, "reconciled": False}
    from . import links
    from .dispatch.allocation import git_admin, git_admin_short

    with git_admin(main, git_admin_short(root, main)):
        again = registration(main, path)
        holds = reconcile_holds(again)
        if not holds and again != info:
            holds = ["the registration changed while it was checked; run reconcile again"]
        if holds:
            return {**again, "holds": holds, "reconciled": False}
        admin = Path(info["admin"])
        if any(links.is_link(entry) for entry in admin.rglob("*")):
            raise AgentError(f"{admin} holds a link; its registration was left in place")
        shutil.rmtree(admin)
    return {**info, "holds": [], "reconciled": True}


def restore(root: Path, name: str, *, head: str, apply: bool = False, sync: bool = True) -> dict[str, Any]:
    """Recreate a lost manager worktree at `head`, its branch's current tip, with its state and
    private links. A moved branch, a new occupant, or a branch checked out elsewhere refuses
    untouched; a present checkout at `head` is already restored."""
    slug = _component(name)
    main = main_checkout(root)
    path = worktree_path(main, slug)
    branch = f"{BRANCH_PREFIX}{slug}"
    holds = restore_holds(root, main, path, branch, head)
    info = registration(main, path)
    result: dict[str, Any] = {"path": str(path), "branch": branch, "head": head, "state": info["state"]}
    if info["state"] == "present" and not holds:
        return {**result, "restored": False, "already": True, "holds": []}
    if holds or not apply:
        return {**result, "restored": False, "holds": holds, "warnings": restore_warnings(info)}
    if info.get("admin"):
        forgot = forget_missing(root, main, path, apply=True)
        if forgot["holds"]:
            return {**result, "restored": False, "holds": forgot["holds"], "warnings": restore_warnings(info)}
    created = create(root, slug, sync=sync, expect_head=head)
    return {
        **result,
        "restored": True,
        "holds": [],
        "steps": created["steps"],
        "warnings": restore_warnings(info),
    }


def restore_holds(root: Path, main: Path, path: Path, branch: str, head: str) -> list[str]:
    """Why a checkout of `branch` at `head` cannot be put back at `path`; shared by manager and
    dispatch restore."""
    info = registration(main, path)
    tip = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], main)
    tip = (tip or "").strip()
    reasons: list[str] = []
    if not tip:
        reasons.append(f"branch {branch} is gone; recover it from its refs/recovery/ pin first")
    elif tip != head:
        reasons.append(
            f"branch {branch} is at {tip}, not the recorded {head}; it moved, so nothing was restored"
        )
    if info["state"] == "present":
        current = repos_mod.git_or_none(["rev-parse", "HEAD"], path)
        if (current or "").strip() != head:
            reasons.append(f"{path} is a live checkout at {(current or '?').strip()}, not {head}")
        return reasons
    if info["state"] != "missing":
        reasons.append(f"path is {info['state']}; a new occupant or unavailable root is never replaced")
    if info.get("admin"):
        if info.get("head") and info["head"] != head:
            reasons.append(f"its registration records HEAD {info['head']}, not {head}")
        reasons += [r for r in reconcile_holds(info) if not r.startswith("path is")]
    for entry in repos_mod.worktrees(main):
        if entry.get("branch") == branch and not _same(entry["path"], path):
            reasons.append(f"branch {branch} is checked out at {entry['path']} (moved?)")
    return reasons


def restore_warnings(info: dict[str, Any]) -> list[str]:
    warnings = [LOST_FILES_WARNING]
    if info.get("staged_ref"):
        warnings.append(f"staged changes are preserved at {info['staged_ref']} and were not reapplied")
    return warnings
