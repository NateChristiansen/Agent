"""One local day of harness usage on this machine, as a schema-1 record.

Tokens come from the Claude transcripts and Codex rollouts only. The dispatch record's `tokens`
field is never read: it is cumulative across attempts and, for Claude, counted once per
streamed content block. Dispatch records are used for two things: which session or thread was
a worker, reviewer or probe, and the day's dispatch and probe dollars by `finished` date, as
`journal.write_generated` computes them.
"""

from __future__ import annotations

import datetime as dt
from datetime import date
from pathlib import Path
from typing import Any

from .. import core
from .claude import claude_usage
from .codex import codex_usage
from .schema import CLASSES, FIELDS, SCHEMA, overall_status
from .scope import Scope, dispatch_index
from .tally import Tally

DAY_OPEN = "collected before the day ended"


def day_end(day: date) -> dt.datetime:
    """Local midnight after `day`: a collection before it cannot be complete."""
    return dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min).astimezone()


def _finished_on(d: dict[str, Any], day: date) -> bool:
    try:
        return dt.datetime.fromisoformat(str(d.get("finished"))).date() == day
    except ValueError:
        return False


def costs(root: Path, day: date, records: list[dict[str, Any]], manager_tokens: int) -> dict[str, Any]:
    from .. import sessions
    from ..dispatch.spend import current_cost

    finished = [d for d in records if d.get("finished") and _finished_on(d, day)]
    work = [current_cost(d) for d in finished if d.get("kind") != "probe"]
    probes = [current_cost(d) for d in finished if d.get("kind") == "probe"]
    manager_usd, manager_complete, manager_found = sessions.cost_summary_for_day(root, day.isoformat())
    return {
        "manager_usd": round(manager_usd, 2),
        "manager_complete": manager_complete if manager_found else manager_tokens == 0,
        "dispatches": len(work),
        "dispatch_usd": round(sum((amount or 0.0 for amount, _ in work), 0.0), 2),
        "probe_usd": round(sum((amount or 0.0 for amount, _ in probes), 0.0), 2),
        "spend_complete": all(source == "provider-result" for _, source in [*work, *probes]),
    }


def _merge(tallies: list[Tally]) -> dict[str, Any]:
    by_class: dict[str, int] = dict.fromkeys(CLASSES, 0)
    models: dict[str, dict[str, int]] = {}
    for tally in tallies:
        for cls, count in tally.by_class.items():
            by_class[cls] += count
        for model, row in tally.models.items():
            into = models.setdefault(model, dict.fromkeys(row, 0))
            for key, value in row.items():
                into[key] += value
    ordered = dict(sorted(models.items(), key=lambda item: -sum(item[1][f] for f in FIELDS)))
    return {
        "total_tokens": sum(by_class.values()),
        "requests": sum(t.requests for t in tallies),
        **{f"tokens_{cls}": by_class[cls] for cls in CLASSES},
        "_models": ordered,
    }


def collect_day(
    root: Path,
    day: date,
    *,
    machine: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """The day's record for this machine. `root` is the vault root; `home` holds `.claude` and,
    unless `CODEX_HOME` says otherwise, `.codex`."""
    from .. import dispatch as dispatch_mod
    from ..dispatch import machine as machine_mod

    home = home or Path.home()
    tag = machine or machine_mod.require_tag(root)
    scope = Scope.for_root(root)
    problems = list(scope.problems)
    try:
        dispatch_records = dispatch_mod.load_all(root)
    except OSError as exc:
        dispatch_records = None
        problems.append(f"dispatch records unreadable, so workers count as other: {exc}")
    index = dispatch_index(dispatch_records or [])
    claude = claude_usage(root, day, scope, index, home)
    codex = codex_usage(day, scope, index, home)
    for tally in (claude, codex):
        if problems and tally.status == "complete":
            tally.status, tally.reason = "partial", problems[0]
    readable = [t for t in (claude, codex) if t.status != "unavailable"]
    cost_fields: dict[str, Any] = {}
    cost_source: dict[str, Any] = {"status": "unavailable", "reason": problems[-1] if problems else ""}
    if dispatch_records is not None:
        try:
            cost_fields = costs(root, day, dispatch_records, sum(t.by_class["manager"] for t in readable))
            cost_source = {"status": "complete"}
        except (OSError, core.AgentError) as exc:
            cost_source["reason"] = f"cannot read session cost records: {exc}"
    sources = {"claude": claude.source(), "codex": codex.source(), "costs": cost_source}
    collected = core.now()
    if collected < day_end(day):
        for source in sources.values():
            if source["status"] == "complete":
                source.update(status="partial", reason=DAY_OPEN)
    record: dict[str, Any] = {
        "tags": ["usage"],
        "schema": SCHEMA,
        "date": day,
        "machine": tag,
        "updated": core.iso(collected),
        "status": overall_status([s["status"] for s in sources.values()]),
    }
    models: dict[str, Any] | None = None
    if readable:
        merged = _merge(readable)
        models = merged.pop("_models")
        record.update(merged)
    record.update(cost_fields)
    if models is not None:
        record["models"] = models
    record["sources"] = sources
    return record
