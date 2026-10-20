# ruff: noqa
from __future__ import annotations
from pathlib import Path
from .check import Check, new_check


def rows(root: Path) -> list[Check]:
    return [*_landing(root), *quarantine(root)]


def _landing(root: Path) -> list[Check]:
    from .. import land_recover

    try:
        lines = land_recover.report(root)
    except Exception as exc:
        return [new_check("landing", "warn", f"could not read the landing state ({type(exc).__name__})")]
    if not lines:
        return [new_check("landing", "ok", "nothing unfinished; main moved only through agent land")]
    return [new_check("landing", "fail" if line.startswith("land ") else "warn", line) for line in lines]


def quarantine(root: Path) -> list[Check]:
    from .. import harness_role, verify_retry
    from ..worktree import main_checkout

    if harness_role.consumer(root):
        return []
    try:
        found = verify_retry.doctor_rows(main_checkout(root))
    except Exception as exc:
        return [new_check("flake quarantine", "warn", f"could not read it ({type(exc).__name__})")]
    return [new_check("flake quarantine", level, detail) for level, detail in found]
