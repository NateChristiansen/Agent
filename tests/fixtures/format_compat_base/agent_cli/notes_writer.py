# ruff: noqa
from __future__ import annotations
from pathlib import Path

WRITER_KEY = "notes_writer"


def write_work_order(root: Path, relative: str, text: str) -> None:
    from .core import AgentError, atomic_write

    if not writes_shared_notes(root):
        raise AgentError("work orders must be written by the configured notes writer checkout")
    atomic_write(root / relative, text, newline="\n")


def writes_shared_notes(root: Path) -> bool:
    from .config_file import LOCAL, local_section
    from .notes import read_notes_root
    from .worktree import is_worktree

    if is_worktree(root):
        return False
    try:
        if read_notes_root(root) is None:
            return True
        if not (root / LOCAL).is_file():
            return False
        return local_section(root, WRITER_KEY) is not False
    except Exception:
        return True
