"""Find a path's git common dir the way git does, without starting git.

`worktree.main_checkout` ran `git rev-parse --git-common-dir` on every call. Fixture roots and
CLI calls hit it several times per command, about 38 ms each, and in the test suite most of those
calls failed (the fixture root is not a repository). This walks up from the path as git's own
discovery does, honouring `GIT_CEILING_DIRECTORIES`. It recognises only layouts it can read with
certainty: a `.git` directory that is a valid repository, or a `.git` file naming a valid gitdir
(with its `commondir`). Anything else returns None, and the caller asks git: `GIT_DIR`,
`GIT_COMMON_DIR` or `GIT_WORK_TREE` set in the environment (even empty), an empty or relative
`GIT_CEILING_DIRECTORIES` entry, a directory that is itself a bare repository, a `.git` directory
with a `commondir`, a HEAD that is neither a ref nor an object id, a gitfile without git's exact
`gitdir: ` prefix, an entry owned by another user (git's safe.directory check), or an unreadable
file (each of those is a case where git and a looser reading disagree). A path in no repository raises,
as git would. Nothing is cached, so a removed or recreated worktree is always seen as it is now.
"""

from __future__ import annotations

import os
from pathlib import Path

from .core import ExternalFailure

_OVERRIDES = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")


_HEX = frozenset("0123456789abcdef")


def _head_ok(path: Path) -> bool:
    """HEAD names a ref under `refs/` or holds a full object id, as git's validation requires."""
    try:
        head = (path / "HEAD").read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return False
    if head.startswith("ref: "):
        return head.removeprefix("ref: ").startswith("refs/")
    return len(head) in (40, 64) and set(head) <= _HEX


def _valid_gitdir(path: Path) -> bool:
    """A repository directory: HEAD, objects and refs, or a linked worktree's `commondir`."""
    try:
        if not (path / "HEAD").is_file() or not _head_ok(path):
            return False
        if (path / "commondir").is_file():
            return True
        return (path / "objects").is_dir() and (path / "refs").is_dir()
    except OSError:
        return False


def _owned(path: Path) -> bool:
    try:
        return path.lstat().st_uid == os.getuid()
    except OSError, AttributeError:
        return False


def _ceilings() -> list[Path] | None:
    """The ceiling directories, resolved; None when an entry is empty or relative (git changes
    how it treats the entries after an empty one, so only git reads those)."""
    raw = os.environ.get("GIT_CEILING_DIRECTORIES")
    if raw is None:
        return []
    out: list[Path] = []
    for item in raw.split(os.pathsep):
        if not item or not os.path.isabs(item):
            return None
        try:
            out.append(Path(item).resolve())
        except OSError:
            return None
    return out


def _common_of(dotgit: Path) -> Path | None:
    """The common dir behind one `.git` entry, or None when it is not certain."""
    if not _owned(dotgit.parent):
        return None
    if dotgit.is_dir() and not dotgit.is_symlink():
        if (dotgit / "commondir").exists():
            return None  # a copied linked-worktree gitdir: git follows its commondir
        return dotgit.resolve() if _valid_gitdir(dotgit) and _owned(dotgit) else None
    if not dotgit.is_file() or not _owned(dotgit):
        return None
    try:
        text = dotgit.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError, UnicodeDecodeError:
        return None
    if not text.startswith("gitdir: ") or "\n" in text:
        return None
    try:
        gitdir = (dotgit.parent / text.removeprefix("gitdir: ")).resolve(strict=True)
    except OSError:
        return None
    if not _valid_gitdir(gitdir) or not _owned(gitdir):
        return None
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return gitdir
    except OSError, UnicodeDecodeError:
        return None
    try:
        path = (gitdir / common).resolve(strict=True)
    except OSError:
        return None
    if not (path / "objects").is_dir() or not (path / "refs").is_dir() or not _owned(path):
        return None
    return path


def common_dir(start: Path) -> Path | None:
    """`start`'s git common dir, resolved; None when only git can say; raises when `start` is in
    no repository (the error `repos.run_git` would raise for `git rev-parse --git-common-dir`)."""
    if any(name in os.environ for name in _OVERRIDES):
        return None
    ceilings = _ceilings()
    if ceilings is None:
        return None
    try:
        here = start.resolve(strict=True)
    except OSError:
        return None  # a missing path: git reports it in its own words
    if not here.is_dir():
        return None
    current = here
    while True:
        dotgit = current / ".git"
        if os.path.lexists(dotgit):
            return _common_of(dotgit)
        if _valid_gitdir(current):
            return None  # a bare repository or a gitdir itself: let git decide
        parent = current.parent
        if parent == current or parent in ceilings:
            break
        current = parent
    raise ExternalFailure(
        f"git rev-parse --git-common-dir in {start}: fatal: not a git repository "
        "(or any of the parent directories): .git"
    )
