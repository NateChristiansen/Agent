"""Links from the harness clone into the notes repo, on every OS.

macOS and Linux use relative symlinks. Plain Windows, without Developer Mode, refuses to make a
symlink. There a directory becomes a junction, which needs no special rights, and a file becomes
a hard link. A hard link has two limits the owner has to live with:

- It cannot cross volumes. `make` refuses a notes repo on another drive when it would need one,
  and says why. A file symlink, when the OS allows it, can cross volumes.
- An atomic replace (write a temp file, rename it over the path) swaps in a new file and
  severs the link, so the two copies then diverge. `recover` fixes that without deleting
  anything when the harness-side copy is stale: it is parked under the ignored
  `.agent/scratch/notes-diverged/` and the link remade. A copy holding an edit is the caller's
  to refuse; it is never parked.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from .core import AgentError, agent_path

DIVERGED_DIR = agent_path(Path(), "scratch", "notes-diverged")

# Read once so a test can simulate plain Windows without patching the global `os.name`, which
# pathlib reads too.
IS_WINDOWS = os.name == "nt"


def is_link(path: Path) -> bool:
    """A symlink or a Windows junction."""
    return path.is_symlink() or path.is_junction()


def link_target(path: Path) -> Path:
    """Where a symlink or junction points, as a normalised absolute path, without following
    it further. A link into a notes repo that has not been rendered yet dangles on purpose, and
    a broken link still has a target worth naming."""
    raw = Path(os.readlink(path))
    if str(raw).startswith("\\\\?\\"):  # a junction's target comes back in NT namespace
        raw = Path(str(raw)[4:])
    if not raw.is_absolute():
        raw = path.parent / raw
    return Path(os.path.normpath(raw))


def is_hard_linked(path: Path, target: Path) -> bool:
    """Two paths to the same file: the Windows file fallback when symlinks are refused."""
    try:
        return path.is_file() and not path.is_symlink() and os.path.samefile(path, target)
    except OSError:
        return False


def points_at(path: Path, target: Path) -> bool:
    """`path` is a link of any kind to `target`."""
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
    """A Windows directory junction, which needs no Developer Mode or admin."""
    if sys.platform != "win32":
        raise AgentError(f"could not link {path} -> {target}: junctions exist only on Windows")
    import _winapi

    _winapi.CreateJunction(str(target.resolve()), str(path))


def make(path: Path, target: Path, *, is_dir: bool) -> str:
    """Link `path` to `target`. Returns the kind made: `symlink`, `junction` or `hardlink`.

    Tries a relative symlink first on every OS, or an absolute one when the target is on another
    Windows drive and no relative path exists. Only on Windows, when that is refused, falls
    back to a junction for a directory or a hard link for a file. A hard link across volumes is
    impossible, so that case raises with the reason instead of silently copying."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_target = os.path.relpath(target, path.parent)
    except ValueError:  # another drive: `relpath` raises instead of returning a path
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
            f"{target} is on another drive, and this Windows account cannot make symlinks, so "
            f"{path.name} would need a hard link, which cannot cross drives. Put the notes repo "
            "on the same drive as the harness, or turn on Windows Developer Mode for symlinks."
        )
    os.link(target, path)
    return "hardlink"


def recover(root: Path, path: Path, target: Path, *, expected: bytes) -> Path:
    """A file at `path` that should be linked to `target` but is a separate copy holding
    `expected`, which the caller has judged stale: park it and relink. Returns where it went,
    under the ignored `.agent/scratch/notes-diverged/`, so nothing new lands in the checkout.

    Nothing is copied or overwritten. The file is moved aside whole, so a write that reaches it
    first travels with it; when the moved bytes are no longer `expected`, it goes back and this
    raises. The link is then made only where nothing exists, so a file written into the gap is
    kept too. Success means `path` is verified linked to `target` as it is now: a notes copy
    replaced meanwhile raises with the parked copy named, never a false success."""
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
            f"{target} was replaced while {path} was being relinked, so the new link is not to the "
            f"current notes copy; this clone's previous copy is at {parked}"
        )
    return parked


def _put_back(parked: Path, path: Path) -> None:
    """Return a moved file to `path` without overwriting anything that has appeared there."""
    try:
        os.link(parked, path)
    except OSError as exc:
        raise AgentError(f"{path} could not be put back ({exc}); this clone's copy is at {parked}") from exc
    parked.unlink()


def _unique(base: Path) -> Path:
    """`base`, or `base.1`, `base.2`, ... the first that does not exist, claimed atomically with
    an exclusive create so two recoveries in the same second never share a name. The claimed
    empty file is then replaced by the move."""
    base.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        candidate = base if n == 0 else base.with_name(f"{base.name}.{n}")
        try:
            with open(candidate, "x"):
                return candidate
        except FileExistsError:
            n += 1
