"""Real symlinks in tests, on a Windows account that may not be allowed to make them.

A test that only needs a link uses `links.make`, which falls back to a junction or a hard link.
A test of what only a real symlink does (a dangling link, a write through a file symlink) calls
`require()` and skips on plain Windows; everywhere else it runs unchanged.
"""

from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path

import pytest

from agent_cli import links

REASON = "this Windows account cannot create symlinks (WinError 1314, privilege not held)"


@functools.cache
def can_symlink() -> bool:
    if os.name != "nt":
        return True
    with tempfile.TemporaryDirectory(prefix="symlink-probe-") as directory:
        try:
            os.symlink("target", Path(directory) / "link")
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                return False
            raise
    return True


def require() -> None:
    if not can_symlink():
        pytest.skip(REASON)


def assert_linked(path: Path, target: Path) -> None:
    """`path` is the link `links.make` made to `target`: exactly a symlink on POSIX, and on
    Windows whichever of symlink, junction or hard link the account allowed."""
    if links.IS_WINDOWS:
        assert links.points_at(path, target), f"{path} is not linked to {target}"
    else:
        assert path.is_symlink(), f"{path} is not a symlink"
