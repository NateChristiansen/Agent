"""Telemetry sink health (see ADR-0038).

Runs the real-append probe against the main checkout's sink, so a doctor run adds one
`health.probe` event. Degraded and failed both warn: telemetry never gates the harness, and the
detail names the status and every observed count.
"""

from __future__ import annotations

from pathlib import Path

from .. import telemetry
from .check import Check, new_check

LEVELS = {"ok": "ok", "degraded": "warn", "failed": "warn"}


def rows(root: Path) -> list[Check]:
    try:
        main = telemetry.sink_root(root)
    except Exception:  # noqa: BLE001 — not a git checkout: no sink to probe, as for worktrees
        return []
    report = telemetry.health(telemetry.Sink.for_main(main))
    counts = ", ".join(f"{name} {value}" for name, value in report["counts"].items())
    newest = report["newest"] or "none"
    detail = f"{report['status']}: {counts}; newest event {newest}; {report['note']}"
    if report["status"] == "failed":
        detail += f"; the probe could not append to {report['sink']}"
    return [new_check("telemetry", LEVELS[report["status"]], detail)]
