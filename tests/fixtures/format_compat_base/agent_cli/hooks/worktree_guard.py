# ruff: noqa
from __future__ import annotations
import os
import sys
from pathlib import Path
from .paths import PROTECTED_PATH_RE, canon, check_path, deny, slashes

MCP_TOOLS_HINT = "Use the agent MCP tools instead: log, status_render, task_add / task_done, feature_use."


def _fold(path: str) -> str:
    return path.lower() if sys.platform in ("darwin", "win32") else path


def _gitdir_of(dotgit: Path) -> Path | None:
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
    admin = _gitdir_of(root / ".git")
    if admin is None or admin.parent.name != "worktrees" or admin.parent.parent.name != ".git":
        return root
    return admin.parent.parent.parent


def _is_worktree_of(child: Path, main: Path) -> bool:
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
    try:
        from ..worktree import worktrees_dir

        main = _main_checkout(root)
        parent_dir = worktrees_dir(main)
        parent = canon(parent_dir)
        p = Path(raw)
        absolute = p if p.is_absolute() else root / p
        written = Path(os.path.normpath(absolute))
        target = canon(absolute)
        if _fold(parent_dir.name) not in _fold(str(written)) and (not target.startswith(parent + os.sep)):
            return None
        child = next((a for a in written.parents if a != a.parent and canon(a.parent) == parent), None)
        if child is None or not _is_worktree_of(child, main):
            return None
        written_rel = slashes(_fold(str(written.relative_to(child))))
        base = canon(child)
        resolved_rel = slashes(target[len(base) + 1 :]) if target.startswith(base + os.sep) else None
        return (written_rel, resolved_rel)
    except Exception:
        return None


def worktree_denial(raw: str, written_rel: str, resolved_rel: str | None) -> str:
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
    p = Path(raw)
    requested = slashes(_fold(os.path.normpath(p if p.is_absolute() else root / p)))
    resolved = slashes(canon(p if p.is_absolute() else root / p))
    if requested != resolved and PROTECTED_PATH_RE.search(rel):
        return deny(f"{raw} lands on {resolved}, a CLI-owned file. {MCP_TOOLS_HINT}")
    return ""
