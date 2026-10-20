"""Read append-only lifecycle telemetry as per-attempt model, owner-wait, lane, verification and
landing spans. Keep resolved base and harness revision with each attempt; mutable dispatch records
alone cannot preserve prior waits. Attribute cost as provider-reported, estimated or unknown.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..records.narrow import as_dict, as_float_or_none, dict_list
from .telemetry_events import main_for, sink_dir

KINDS = ("dispatch.launch", "dispatch.transition", "dispatch.terminal")


def _events(root: Path, dispatch_id: str) -> list[dict[str, Any]]:
    main = main_for(root)
    if main is None:
        return []
    found: list[dict[str, Any]] = []
    for day in sorted((sink_dir(main) / "events").glob("*.jsonl")):
        try:
            lines = day.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if dispatch_id not in line:
                continue
            try:
                event = as_dict(json.loads(line))
            except ValueError:
                continue
            if event.get("dispatch") == dispatch_id and event.get("kind") in KINDS:
                found.append(event)
    return sorted(found, key=lambda e: str(e.get("ts") or ""))


def _when(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def spans(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    """Each transition of `d` with the seconds until the next one (the last runs to now unless
    it ended the dispatch), the total time by phase, and each attempt's cost and its source."""
    rows: list[dict[str, Any]] = []
    for event in _events(root, str(d["id"])):
        if event.get("kind") != "dispatch.transition":
            continue
        data = as_dict(event.get("data"))
        extra = {k: v for k, v in data.items() if k not in ("phase", "attempt", "base")}
        rows.append(
            {
                "attempt": data.get("attempt"),
                "phase": data.get("phase"),
                "ts": event.get("ts"),
                "base": data.get("base"),
                "harness": event.get("commit"),
                **({"detail": extra} if extra else {}),
            }
        )
    by_phase: dict[str, float] = {}
    for i, row in enumerate(rows):
        start = _when(row["ts"])
        last = i + 1 == len(rows)
        if last and row["phase"] in ("ended", "landed"):
            row["seconds"] = None
            continue
        end = datetime.now(UTC) if last else _when(rows[i + 1]["ts"])
        row["seconds"] = round((end - start).total_seconds(), 1) if start and end else None
        if row["seconds"] is not None and row["phase"] != "ended":  # idle until a resume, not lifecycle time
            by_phase[str(row["phase"])] = round(by_phase.get(str(row["phase"]), 0.0) + row["seconds"], 1)
    return {"dispatch": d["id"], "spans": rows, "by_phase": by_phase, "costs": costs(d)}


def costs(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Each attempt's own spend and where the figure came from: `provider`, `estimate` or
    `unknown`. Kept apart, never summed into one number that hides an unknown."""
    attempts = as_dict(d.get("cost_attempts"))
    evidence = as_dict(d.get("cost_evidence"))
    reported = {  # the worker's thread line for each attempt carries that attempt's own cost
        row.get("attempt"): as_float_or_none(row.get("cost_usd"))
        for row in dict_list(d.get("thread"))
        if row.get("from") == "worker"
    }
    out: list[dict[str, Any]] = []
    for n in range(1, int(d.get("attempt") or 1) + 1):
        added = as_float_or_none(as_dict(attempts.get(str(n))).get("added"))
        usd = added if added is not None else reported.get(n)
        out.append({"attempt": n, "usd": usd, "source": str(evidence.get(str(n)) or "unknown")})
    return out


def text(data: dict[str, Any]) -> str:
    lines = [f"{data['dispatch']} lifecycle"]
    for row in data["spans"]:
        took = f"{row['seconds']:.0f} s" if row.get("seconds") is not None else "-"
        lines.append(f"  attempt {row['attempt']} {row['phase']:<22} {row['ts']}  {took}")
    if not data["spans"]:
        lines.append("  no transitions recorded (dispatches before then have none)")
    if data["by_phase"]:
        lines.append("  by phase: " + ", ".join(f"{k} {v:.0f} s" for k, v in data["by_phase"].items()))
    for row in data["costs"]:
        usd = f"${row['usd']:.2f}" if row["usd"] is not None else "unknown"
        lines.append(f"  attempt {row['attempt']} cost {usd} ({row['source']})")
    return "\n".join(lines)
