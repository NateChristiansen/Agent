# ruff: noqa
from __future__ import annotations
from pathlib import Path
from .. import telemetry
from .check import Check, new_check

LEVELS = {"ok": "ok", "degraded": "warn", "failed": "warn"}


def rows(root: Path) -> list[Check]:
    try:
        main = telemetry.sink_root(root)
    except Exception:
        return []
    report = telemetry.health(telemetry.Sink.for_main(main))
    counts = ", ".join((f"{name} {value}" for name, value in report["counts"].items()))
    newest = report["newest"] or "none"
    detail = f"{report['status']}: {counts}; newest event {newest}; {report['note']}"
    if report["status"] == "failed":
        detail += f"; the probe could not append to {report['sink']}"
    return [new_check("telemetry", LEVELS[report["status"]], detail)]
