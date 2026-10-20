# ruff: noqa
from __future__ import annotations
import os
import sys
import time
from pathlib import Path
from .core import AgentError, agent_path

DIVERGED_DIR = agent_path(Path(), "scratch", "notes-diverged")
IS_WINDOWS = os.name == "nt"


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def link_target(path: Path) -> Path:
    raw = Path(os.readlink(path))
    if str(raw).startswith("\\\\?\\"):
        raw = Path(str(raw)[4:])
    if not raw.is_absolute():
        raw = path.parent / raw
    return Path(os.path.normpath(raw))


def is_hard_linked(path: Path, target: Path) -> bool:
    try:
        return path.is_file() and (not path.is_symlink()) and os.path.samefile(path, target)
    except OSError:
        return False


def points_at(path: Path, target: Path) -> bool:
    if is_link(path):
        return link_target(path) == Path(os.path.normpath(target))
    return is_hard_linked(path, target)


def _same_volume(a: Path, b: Path) -> bool:
    if not IS_WINDOWS:
        return True
    return (
        os.path.splitdrive(os.path.abspath(a))[0].lower() == os.path.splitdrive(os.path.abspath(b))[0].lower()
    )


def _junction(path: Path, target: Path) -> None:
    if sys.platform != "win32":
        raise AgentError(f"could not link {path} -> {target}: junctions exist only on Windows")
    import _winapi

    _winapi.CreateJunction(str(target.resolve()), str(path))


def make(path: Path, target: Path, *, is_dir: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_target = os.path.relpath(target, path.parent)
    except ValueError:
        link_target = str(target.resolve())
    try:
        os.symlink(link_target, path, target_is_directory=is_dir)
        return "symlink"
    except OSError as exc:
        if not IS_WINDOWS:
            raise AgentError(f"could not link {path} -> {target}: {exc}") from exc
    if is_dir:
        _junction(path, target)
        return "junction"
    if not _same_volume(path, target):
        raise AgentError(
            f"{target} is on another drive, and this Windows account cannot make symlinks, so {path.name} would need a hard link, which cannot cross drives. Put the notes repo on the same drive as the harness, or turn on Windows Developer Mode for symlinks."
        )
    os.link(target, path)
    return "hardlink"


def recover(root: Path, path: Path, target: Path, *, expected: bytes) -> Path:
    parked = _unique(root / DIVERGED_DIR / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(path, parked)
    except BaseException:
        parked.unlink(missing_ok=True)
        raise
    try:
        moved = parked.read_bytes()
    except OSError:
        moved = None
    if moved != expected:
        _put_back(parked, path)
        raise AgentError(f"{path} changed while it was being relinked; it is back in place, unlinked")
    try:
        make(path, target, is_dir=False)
    except BaseException:
        _put_back(parked, path)
        raise
    if not points_at(path, target):
        raise AgentError(
            f"{target} was replaced while {path} was being relinked, so the new link is not to the current notes copy; this clone's previous copy is at {parked}"
        )
    return parked


def _put_back(parked: Path, path: Path) -> None:
    try:
        os.link(parked, path)
    except OSError as exc:
        raise AgentError(f"{path} could not be put back ({exc}); this clone's copy is at {parked}") from exc
    parked.unlink()


def _unique(base: Path) -> Path:
    base.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        candidate = base if n == 0 else base.with_name(f"{base.name}.{n}")
        try:
            with open(candidate, "x"):
                return candidate
        except FileExistsError:
            n += 1
