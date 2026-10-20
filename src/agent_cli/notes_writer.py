"""Restrict rendered shared-note writes to the configured harness checkout. Worktrees and probe clones
can read notes without claiming the writer role.

A shared-notes writer must be a main checkout with machine-local `agent.local.yaml` and no
`notes_writer: false` opt-out. Setup creates that untracked file; clones cannot inherit the role
accidentally. No first-claim marker can lock the intended writer out. Without linked notes every
checkout may render.
"""

from __future__ import annotations

from pathlib import Path

WRITER_KEY = "notes_writer"


def write_work_order(root: Path, relative: str, text: str) -> None:
    """Write a composed order through the vault's notes link, only from its notes writer."""
    from .core import AgentError, atomic_write

    if not writes_shared_notes(root):
        raise AgentError("work orders must be written by the configured notes writer checkout")
    atomic_write(root / relative, text, newline="\n")


def writes_shared_notes(root: Path) -> bool:
    """Whether a render from `root` may rewrite fields in the shared notes. Never raises: when it
    cannot tell, a linked worktree answers False and anything else True, as before this check."""
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
    except Exception:  # noqa: BLE001 — a check that cannot run keeps the old behaviour
        return True
