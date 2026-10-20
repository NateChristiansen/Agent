# ruff: noqa
from __future__ import annotations
import datetime as dt
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any
from ..records.narrow import as_dict, as_float, as_int
from .schema import CLASSES, MODEL_KEYS, checkpoint_problems, record_problems, stamp

COUNTED = ("complete", "inactive")


def covers_day(checkpoint: Mapping[str, object], day: date) -> bool:
    covered = stamp(checkpoint.get("covered_through"))
    if covered is None:
        return False
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=covered.tzinfo)
    return covered >= end


def machine_status(
    day: date, record: Mapping[str, object] | None, checkpoint: Mapping[str, object] | None
) -> dict[str, Any]:
    if record is not None:
        problems = record_problems(record)
        if not problems and str(record.get("date")) == day.isoformat():
            return {"status": record["status"], "updated": record["updated"]}
        return {"status": "missing", "reason": problems[0] if problems else "record is for another day"}
    if checkpoint is not None and (not checkpoint_problems(checkpoint)) and covers_day(checkpoint, day):
        return {"status": "inactive", "updated": checkpoint["updated"]}
    return {"status": "missing"}


def aggregate(
    day: date,
    expected: Iterable[str],
    records: Mapping[str, Mapping[str, object]],
    checkpoints: Mapping[str, Mapping[str, object]],
) -> dict[str, Any]:
    machines = sorted({*expected, *records, *checkpoints})
    usage_machines = {m: machine_status(day, records.get(m), checkpoints.get(m)) for m in machines}
    counted = [records[m] for m in machines if m in records and usage_machines[m]["status"] != "missing"]
    by_class = dict.fromkeys(CLASSES, 0)
    models: dict[str, dict[str, int]] = {}
    for record in counted:
        for cls in CLASSES:
            by_class[cls] += as_int(record.get(f"tokens_{cls}"))
        for model, row in as_dict(record.get("models")).items():
            into = models.setdefault(model, dict.fromkeys(MODEL_KEYS, 0))
            for key in MODEL_KEYS:
                into[key] += as_int(as_dict(row).get(key))
    with_costs = [r for r in counted if "dispatch_usd" in r]
    every_cost = len(with_costs) == len(counted)
    return {
        "usage_machines": usage_machines,
        "tokens_complete": all((entry["status"] in COUNTED for entry in usage_machines.values())),
        "session_tokens": sum((as_int(r.get("total_tokens")) for r in counted)),
        "requests": sum((as_int(r.get("requests")) for r in counted)),
        **{f"tokens_{cls}": by_class[cls] for cls in CLASSES},
        "models": models,
        "manager_usd": round(sum((as_float(r.get("manager_usd")) for r in with_costs)), 2),
        "manager_complete": every_cost and all((r.get("manager_complete") is True for r in with_costs)),
        "dispatches": sum((as_int(r.get("dispatches")) for r in with_costs)),
        "spend_usd": round(sum((as_float(r.get("dispatch_usd")) for r in with_costs)), 2),
        "probe_spend_usd": round(sum((as_float(r.get("probe_usd")) for r in with_costs)), 2),
        "spend_complete": every_cost and all((r.get("spend_complete") is True for r in with_costs)),
    }
