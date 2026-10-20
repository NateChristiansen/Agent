"""Landing state for doctor (see ADR-0041): unfinished landings, main moves that were not
`agent land`, a main tip with no full pass record, and the flake quarantine's expired or
unapproved entries."""

from __future__ import annotations

from pathlib import Path

from .check import Check, new_check


def rows(root: Path) -> list[Check]:
    return [*_landing(root), *quarantine(root)]


def _landing(root: Path) -> list[Check]:
    from .. import land_recover

    try:
        lines = land_recover.report(root)
    except Exception as exc:  # noqa: BLE001 — doctor reports, it never raises
        return [new_check("landing", "warn", f"could not read the landing state ({type(exc).__name__})")]
    if not lines:
        return [new_check("landing", "ok", "nothing unfinished; main moved only through agent land")]
    return [new_check("landing", "fail" if line.startswith("land ") else "warn", line) for line in lines]


def quarantine(root: Path) -> list[Check]:
    """`tests/quarantine.toml` in the main checkout: a malformed entry fails, and an expired or
    unapproved one is named, because it gates like any other test. A consumer install never runs
    the harness's own suite, so it has no quarantine to approve and the rows are left out."""
    from .. import harness_role, verify_retry
    from ..worktree import main_checkout

    if harness_role.consumer(root):
        return []
    try:
        found = verify_retry.doctor_rows(main_checkout(root))
    except Exception as exc:  # noqa: BLE001 — doctor reports, it never raises
        return [new_check("flake quarantine", "warn", f"could not read it ({type(exc).__name__})")]
    return [new_check("flake quarantine", level, detail) for level, detail in found]
