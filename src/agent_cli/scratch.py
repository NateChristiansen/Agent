"""Scratch space that lives outside the vault: plan-mode drafts, dispatch work orders, and
journal drafts.

`.agent/scratch/` is gitignored (it sits under `.agent/`, already ignored wholesale) and kept out
of Obsidian's file explorer and search, since none of it is the owner's.
"""

from __future__ import annotations

from pathlib import Path

from .core import agent_path

SCRATCH_DIR = agent_path(Path(), "scratch")
SUBDIRS = ("plans", "dispatch", "journal")


def scratch_dir(root: Path) -> Path:
    return root / SCRATCH_DIR


def ensure(root: Path) -> Path:
    """Create `.agent/scratch/{plans,dispatch,journal}` (idempotent) and return the scratch
    root, absolute."""
    base = scratch_dir(root)
    for name in SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base
