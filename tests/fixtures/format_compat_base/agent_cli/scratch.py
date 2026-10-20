# ruff: noqa
from __future__ import annotations
from pathlib import Path
from .core import agent_path

SCRATCH_DIR = agent_path(Path(), "scratch")
SUBDIRS = ("plans", "dispatch", "journal")


def scratch_dir(root: Path) -> Path:
    return root / SCRATCH_DIR


def ensure(root: Path) -> Path:
    base = scratch_dir(root)
    for name in SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base
