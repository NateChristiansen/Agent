"""Landing state for doctor (see ADR-0041): unfinished landings, main moves that were not
`agent land`, a main tip with no full pass record, the flake quarantine's expired or
unapproved entries, and a suite budget left looser than its headroom."""

from __future__ import annotations

from pathlib import Path

from .check import Check, new_check


def rows(root: Path) -> list[Check]:
    return [*_landing(root), *quarantine(root), *budget_slack(root)]


def budget_slack(root: Path) -> list[Check]:
    """Main's suite budget against the latest landing's record: a limit more than twice the
    headroom above its figure lets the suite grow unseen, so the row names the tighter value. No
    row when either is missing; never a failure."""
    from .. import land_journal, suite_budget, verify_record
    from ..worktree import main_checkout

    name = "suite budget"
    try:
        main = main_checkout(root)
        try:
            text = (main / suite_budget.PATH).read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        budget = suite_budget.parse(text)
        landed = [j for j in land_journal.every(main) if j.get("state") == "done"]
        run_id = str(landed[-1].get("record") or "") if landed else ""
        record = verify_record.by_run(root, [run_id]).get(run_id)
        found = suite_budget.slack(record, budget) if record is not None else []
    except Exception as exc:  # noqa: BLE001 — doctor reports, it never raises
        return [
            new_check(name, "warn", f"could not compare it with the latest landing ({type(exc).__name__})")
        ]
    if not found:
        return []
    head = budget.limits["headroom_pct"]
    loose = [
        f"{key} = {limit:g} sits {limit - value:g} above the latest {value:,}; write {key} = {tight}"
        for key, value, limit, tight in found
        if tight is not None
    ]
    if loose:
        return [
            new_check(
                name,
                "warn",
                f"slack above twice the {head:g}% headroom in {suite_budget.PATH} [suite]: "
                + "; ".join(loose),
            )
        ]
    return [
        new_check(name, "ok", f"limits within twice the {head:g}% headroom of landing {landed[-1].get('id')}")
    ]


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
