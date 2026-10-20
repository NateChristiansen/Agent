"""`protect-generated` for manager worktrees under `../Agent-worktrees/`.

Asking "outside the harness root" there put a prompt on every worktree edit, which no allow rule
could clear. `manager_worktree` recognizes a worktree; `worktree_denial`
judges a path inside one the way the main checkout judges its own; `alias_denial` judges a
symlinked path by where it lands. guards.py wires them into `protect_generated`.

The permission boundary. Setup allows Edit and Write under the whole parent, so this hook is
the only thing that still asks there, and it stops asking only for a path inside an existing
direct child of the parent that is a verified linked worktree of this repository: the child's
`.git` file names an admin dir under the main checkout's `.git/worktrees/`, and that admin
dir names the child back. Git writes both halves in `git worktree add`, so a model cannot make
a directory pass by creating it or by copying a `.git` file into it. An absent child (a made-up
name), a plain directory, a look-alike, anything outside the parent, and `../` out of a
worktree all keep the "outside the harness root" ask. Inside a verified worktree the in-root
rules apply to its relative path: CLI-owned files and `repos/` are denied (Claude's native
deny rules are anchored at the project root and never see a worktree). A registered repo's
path and its `worktrees_dir` are refused before any of this, so a worker's worktree under the
same parent stays off limits. Where a symlink is involved the resolved path decides: one that
lands in the main checkout or another worktree is judged there, and one that lands on a
CLI-owned file is denied however it was spelled (`alias_denial`).

Everything here is file reads: a PreToolUse hook that runs git on every edit is the kind that
times out and fails open.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .paths import PROTECTED_PATH_RE, canon, check_path, deny, slashes

MCP_TOOLS_HINT = "Use the agent MCP tools instead: log, status_render, task_add / task_done, feature_use."


def _fold(path: str) -> str:
    """Case-folded the way `canon` folds, for a path that is not resolved."""
    return path.lower() if sys.platform in ("darwin", "win32") else path


def _gitdir_of(dotgit: Path) -> Path | None:
    """The admin directory a linked worktree's one-line `.git` file names
    (`gitdir: <main>/.git/worktrees/<name>`), or None for a directory or anything else."""
    try:
        if not dotgit.is_file() or dotgit.stat().st_size > 4096:
            return None
        text = dotgit.read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return None
    if not text.startswith("gitdir:"):
        return None
    admin = Path(text[len("gitdir:") :].strip())
    return admin if admin.is_absolute() else dotgit.parent / admin


def _main_checkout(root: Path) -> Path:
    """The main checkout `root` belongs to, read from `root/.git`. `root` itself when it is
    the main checkout or not a git checkout, as `worktree_grants.parent_for` falls back."""
    admin = _gitdir_of(root / ".git")
    if admin is None or admin.parent.name != "worktrees" or admin.parent.parent.name != ".git":
        return root
    return admin.parent.parent.parent


def _is_worktree_of(child: Path, main: Path) -> bool:
    """`child` is a linked worktree of `main`'s repository: its `.git` file names an admin dir
    under `main/.git/worktrees/`, and that admin dir's `gitdir` names `child/.git` back. The
    back-pointer rules out a `.git` file copied into a look-alike directory."""
    dotgit = child / ".git"
    admin = _gitdir_of(dotgit)
    if admin is None or canon(admin.parent) != canon(main / ".git" / "worktrees"):
        return False
    try:
        back = Path((admin / "gitdir").read_text(encoding="utf-8").strip())
    except OSError, UnicodeDecodeError:
        return False
    return canon(back if back.is_absolute() else admin / back) == canon(dotgit)


def manager_worktree(root: Path, raw: str) -> tuple[str, str | None] | None:
    """`(written_rel, resolved_rel)` when `raw` lies inside a manager worktree, else None.

    A manager worktree is an existing direct child of `worktree.worktrees_dir(main)` that is a
    linked git worktree of this repository (`_is_worktree_of`); see the module docstring for
    why an absent or unverified child is not one. `written_rel` is the path as written, relative to the worktree, so a CLI-owned
    name is caught whatever a symlink makes of it. `resolved_rel` is None when the resolved path
    leaves the worktree. Never raises: an error reads as "not a worktree", the stricter answer.

    A worker's dispatch worktree under the parent never reaches here: the caller refuses a
    registered repo's path and its `worktrees_dir` first."""
    try:
        from ..worktree import worktrees_dir

        main = _main_checkout(root)
        parent_dir = worktrees_dir(main)
        parent = canon(parent_dir)
        p = Path(raw)
        absolute = p if p.is_absolute() else root / p
        written = Path(os.path.normpath(absolute))
        # Where the write lands: the ORIGINAL path resolved. `normpath` first would collapse
        # `link/..` by text before the OS follows `link`: `wt/link/../ordinary` read as
        # `wt/ordinary` while it landed on main's STATUS.md.
        target = canon(absolute)
        if _fold(parent_dir.name) not in _fold(str(written)) and not target.startswith(parent + os.sep):
            return None  # the cheap answer for every ordinary path
        child = next((a for a in written.parents if a != a.parent and canon(a.parent) == parent), None)
        if child is None or not _is_worktree_of(child, main):
            return None
        written_rel = slashes(_fold(str(written.relative_to(child))))
        base = canon(child)
        resolved_rel = slashes(target[len(base) + 1 :]) if target.startswith(base + os.sep) else None
        return written_rel, resolved_rel
    except Exception:  # noqa: BLE001 — a hook never raises; "not a worktree" keeps the ask
        return None


def worktree_denial(raw: str, written_rel: str, resolved_rel: str | None) -> str:
    """The in-root checks on a worktree path: CLI-owned files, then `repos/`. Claude's native
    `Edit(/STATUS.md)`-style deny rules are anchored at the project root and never see a
    worktree, so the CLI-owned names are denied here from the regex the Bash half uses. No
    lease: leases cover the shared checkout, and a manager worktree is its own copy."""
    for rel in (written_rel, resolved_rel):
        if rel is None:
            continue
        if PROTECTED_PATH_RE.search(rel):
            return deny(f"{raw} is a CLI-owned file in a manager worktree. {MCP_TOOLS_HINT}")
        denial = check_path(rel)
        if denial:
            return denial
    return ""


def alias_denial(root: Path, raw: str, rel: str) -> str:
    """Deny an in-root write whose resolved path `rel` is CLI-owned when the requested path is
    spelled differently (a symlink). The in-root branch leaves CLI-owned files to Claude's
    native deny rules, which match the requested path and so never see the alias: a worktree's
    `ordinary -> <main>/STATUS.md` slipped through.

    `rel` comes from resolving the original path, so it is the true landing even for
    `a/link/../STATUS.md`. The text-normalized spelling is only the comparison: when it equals
    the landing, the write goes exactly where the text says and the native rules, which match
    that spelling, cover it (`features/../STATUS.md` stays theirs, as test_hooks_guards
    expects). When the two differ, a symlink moved the write and this hook judges the landing."""
    p = Path(raw)
    requested = slashes(_fold(os.path.normpath(p if p.is_absolute() else root / p)))
    resolved = slashes(canon(p if p.is_absolute() else root / p))
    if requested != resolved and PROTECTED_PATH_RE.search(rel):
        return deny(f"{raw} lands on {resolved}, a CLI-owned file. {MCP_TOOLS_HINT}")
    return ""
