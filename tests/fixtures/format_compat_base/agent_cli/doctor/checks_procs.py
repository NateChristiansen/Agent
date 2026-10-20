# ruff: noqa
from __future__ import annotations
from pathlib import Path
from .check import Check, new_check


def check_procs(root: Path) -> list[Check]:
    from .. import procs

    try:
        rows = procs.classify(root, procs.snapshot())
    except Exception as exc:
        return [new_check("processes", "warn", f"could not read the process table: {exc}")]
    orphans = [r for r in rows if r["orphan"]]
    stuck = [r for r in rows if r["stuck_supervisor"]]
    kept = [r for r in rows if r["kept"]]
    out: list[Check] = []
    if orphans:
        names = ", ".join((f"{r['pid']} ({r['owner']}, up {r['elapsed']})" for r in orphans[:6]))
        more = f" and {len(orphans) - 6} more" if len(orphans) > 6 else ""
        out.append(
            new_check(
                "processes",
                "warn",
                f"{len(orphans)} orphaned: {names}{more} — `agent procs list` shows them, `agent procs reap` kills them, `agent procs keep <pid>` spares one",
            )
        )
    if stuck:
        names = ", ".join((f"{r['pid']} for {r['supervisor_of']}" for r in stuck))
        out.append(
            new_check(
                "processes",
                "warn",
                f"supervisor still running for a dispatch that is not: {names} — check `agent dispatch show`; it is never reaped automatically",
            )
        )
    if not out:
        detail = f"{len(rows)} harness-owned, none orphaned"
        if kept:
            detail += f"; {len(kept)} kept: {', '.join((str(r['pid']) for r in kept))}"
        out.append(new_check("processes", "ok", detail))
    return out
