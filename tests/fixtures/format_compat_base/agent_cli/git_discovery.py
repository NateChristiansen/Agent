# ruff: noqa
from __future__ import annotations
import os
from pathlib import Path
from .core import ExternalFailure

_OVERRIDES = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")
_HEX = frozenset("0123456789abcdef")


def _head_ok(path: Path) -> bool:
    try:
        head = (path / "HEAD").read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return False
    if head.startswith("ref: "):
        return head.removeprefix("ref: ").startswith("refs/")
    return len(head) in (40, 64) and set(head) <= _HEX


def _valid_gitdir(path: Path) -> bool:
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
    if not _owned(dotgit.parent):
        return None
    if dotgit.is_dir() and (not dotgit.is_symlink()):
        if (dotgit / "commondir").exists():
            return None
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
    if not (path / "objects").is_dir() or not (path / "refs").is_dir() or (not _owned(path)):
        return None
    return path


def common_dir(start: Path) -> Path | None:
    if any((name in os.environ for name in _OVERRIDES)):
        return None
    ceilings = _ceilings()
    if ceilings is None:
        return None
    try:
        here = start.resolve(strict=True)
    except OSError:
        return None
    if not here.is_dir():
        return None
    current = here
    while True:
        dotgit = current / ".git"
        if os.path.lexists(dotgit):
            return _common_of(dotgit)
        if _valid_gitdir(current):
            return None
        parent = current.parent
        if parent == current or parent in ceilings:
            break
        current = parent
    raise ExternalFailure(
        f"git rev-parse --git-common-dir in {start}: fatal: not a git repository (or any of the parent directories): .git"
    )
