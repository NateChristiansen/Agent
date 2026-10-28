"""The suite budget in the budget file (`PATH`), and the landing check against it.

The file's shape:

    [suite]       tests_max, spawns_max, headroom_pct
    [[raise]]     date, from, to, reason, approved_by
    [slow]        "<test node id>" = its call-phase spawn ceiling
    [sleep]       "<test file>" = its time.sleep count
    [weak]        "<test node id>" = why it may stay assertion-free or mock-only
    [[skip]]      node, reason, expires (ISO date)

`check` refuses a pass record whose full pytest command ran more tests or started more processes
than the candidate's limits, or could not count either, and any loosening of main's file, compared
entry by entry. A matching `budget_raise`, the reason of a `[[raise]]` entry the candidate adds,
allows exceptions. Numeric raises from an existing suite limit bound that metric by `to`.
While main has no budget file nothing is enforced.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .records.narrow import as_dict, as_dict_or_none, as_float_or_none, as_list_or_none, str_list

PATH = "tests/budget.toml"
LIMITS = ("tests_max", "spawns_max", "headroom_pct")
FIGURES = (("tests", "tests_max"), ("spawns", "spawns_max"))
#: How many entries of one kind a refusal names before it counts the rest.
SHOWN = 10


class BudgetError(ValueError):
    """The budget file does not parse or does not have the expected shape."""


@dataclass(frozen=True)
class Budget:
    limits: dict[str, float]
    raises: list[dict[str, object]]
    slow: dict[str, float]
    sleep: dict[str, float]
    weak: frozenset[str]
    skip: dict[str, date]


def _number(value: object, where: str) -> float:
    # Every comparison with nan is false, so a nan limit would pass any figure.
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise BudgetError(f"{where} must be a finite number, not {value!r}")
    return value


def _date(value: object, where: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise BudgetError(f"{where} must be an ISO date, not {value!r}") from None


def _table(data: dict[str, Any], name: str) -> dict[str, object]:
    value = as_dict_or_none(data.get(name, {}))
    if value is None:
        raise BudgetError(f"[{name}] must be a table")
    return value


def _tables(data: dict[str, Any], name: str) -> list[dict[str, object]]:
    value = as_list_or_none(data.get(name, []))
    tables = [t for v in value or [] if (t := as_dict_or_none(v)) is not None]
    if value is None or len(tables) != len(value):
        raise BudgetError(f"[[{name}]] must be an array of tables")
    return tables


def parse(text: str) -> Budget:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BudgetError(str(exc)) from None
    suite = _table(data, "suite")
    limits = {key: _number(suite.get(key), f"[suite] {key}") for key in LIMITS}
    counts = {
        name: {node: _number(v, f"[{name}] {node!r}") for node, v in _table(data, name).items()}
        for name in ("slow", "sleep")
    }
    listed = as_list_or_none(data.get("weak"))
    weak = str_list(listed) if listed is not None else list(_table(data, "weak"))
    skip: dict[str, date] = {}
    for i, entry in enumerate(_tables(data, "skip")):
        node = entry.get("node")
        if not isinstance(node, str) or not node:
            raise BudgetError(f"[[skip]] entry {i + 1} has no node")
        expires = _date(entry.get("expires"), f"[[skip]] {node!r} expires")
        skip[node] = max(expires, skip.get(node, expires))
    return Budget(limits, _tables(data, "raise"), counts["slow"], counts["sleep"], frozenset(weak), skip)


def _shown(lines: list[str]) -> list[str]:
    if len(lines) <= SHOWN:
        return lines
    return [*lines[:SHOWN], f"and {len(lines) - SHOWN} more like these"]


def _fmt(value: float) -> str:
    return f"{value:,}" if isinstance(value, int) else f"{value:g}"


def loosenings(main: Budget, cand: Budget) -> list[str]:
    """Each way `cand` is looser than `main`. Removing entries and lowering numbers tighten."""
    out = [
        f"[suite] {key} raised from {_fmt(main.limits[key])} to {_fmt(cand.limits[key])}"
        for key in LIMITS
        if cand.limits[key] > main.limits[key]
    ]
    for name, old, new in (("slow", main.slow, cand.slow), ("sleep", main.sleep, cand.sleep)):
        found: list[str] = []
        for key in sorted(new):
            if key not in old:
                found.append(f"[{name}] adds {key!r}")
            elif new[key] > old[key]:
                found.append(f"[{name}] {key!r} raised from {_fmt(old[key])} to {_fmt(new[key])}")
        out += _shown(found)
    out += _shown([f"[weak] adds {key!r}" for key in sorted(cand.weak - main.weak)])
    found = []
    for node in sorted(cand.skip):
        if node not in main.skip:
            found.append(f"[[skip]] adds {node!r}")
        elif cand.skip[node] > main.skip[node]:
            found.append(f"[[skip]] {node!r} expires later, {main.skip[node]} to {cand.skip[node]}")
    return out + _shown(found)


def overages(record: dict[str, Any], budget: Budget) -> list[str]:
    """Each suite figure of the record that is over its limit, or that the record could not measure."""
    suite = as_dict(record.get("suite"))
    why = "; ".join(str_list(suite.get("missing"))) or "the record has no suite figures"
    out: list[str] = []
    for figure, limit_key in FIGURES:
        value = suite.get(figure)
        limit = budget.limits[limit_key]
        if isinstance(value, bool) or not isinstance(value, int):
            out.append(f"the record has no suite.{figure} ({why})")
        elif value > limit:
            out.append(
                f"suite.{figure} {_fmt(value)} exceeds {limit_key} {_fmt(limit)} by {_fmt(value - limit)}"
            )
    return out


def figures(record: dict[str, Any] | None) -> dict[str, Any]:
    """The record's tests, spawns and wall seconds, each None where the record holds none."""
    suite = as_dict(as_dict(record).get("suite"))
    counts = {
        f: v if isinstance(v := suite.get(f), int) and not isinstance(v, bool) else None for f, _ in FIGURES
    }
    return {**counts, "wall_s": as_float_or_none(suite.get("wall_s"))}


def trend(root: Path, journals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each landing's id, date and suite figures, read from the pass record it landed on."""
    from . import verify_record

    kept = verify_record.by_run(root, (str(j.get("record") or "") for j in journals))
    return [
        {"id": j.get("id"), "date": j.get("ts"), **figures(kept.get(str(j.get("record") or "")))}
        for j in journals
    ]


def shown(row: dict[str, Any]) -> str:
    """`row`'s figures in a line; an unknown one is `?`, never 0."""
    tests, spawns, wall = row.get("tests"), row.get("spawns"), row.get("wall_s")
    return (
        f"{'?' if tests is None else _fmt(tests)} tests, {'?' if spawns is None else _fmt(spawns)} spawns, "
        f"{'?' if wall is None else f'{wall:.0f}'} s"
    )


def slack(record: dict[str, Any], budget: Budget) -> list[tuple[str, int, float, int | None]]:
    """Per limit: its key, the record's figure, the limit, and the tightened limit to write when the
    limit sits more than twice `headroom_pct` above the figure (else None). A figure the record
    lacks is left out."""
    headroom = budget.limits["headroom_pct"]
    out: list[tuple[str, int, float, int | None]] = []
    for figure, key in FIGURES:
        value = figures(record)[figure]
        if value is None:
            continue
        limit = budget.limits[key]
        tight = math.ceil(value * (100 + headroom) / 100)
        out.append((key, value, limit, tight if limit * 100 > value * (100 + 2 * headroom) else None))
    return out


def check(
    record: dict[str, Any] | None,
    main_text: str | None,
    cand_text: str | None,
    budget_raise: str | None = None,
) -> list[str]:
    """Why the candidate may not land against main's budget, or nothing. `main_text` and
    `cand_text` are each tree's budget file, None where the tree has none. With no `record` only
    the files are compared."""
    if main_text is None:
        return []
    problems: list[str] = []
    main: Budget | None = None
    try:
        main = parse(main_text)
    except BudgetError as exc:
        problems.append(f"main's {PATH} cannot be read: {exc}")
    cand: Budget | None = None
    if cand_text is None:
        problems.append(f"the candidate deletes {PATH}")
    else:
        try:
            cand = parse(cand_text)
        except BudgetError as exc:
            return [f"the candidate's {PATH} cannot be read: {exc}"]
        if main is not None:
            problems += loosenings(main, cand)
    # A deleted file is held to main's limits.
    if record is not None and (limits := cand or main):
        problems += overages(record, limits)
    if budget_raise is None:
        return problems
    before = main.raises if main is not None else []
    added = [r for r in cand.raises if r not in before] if cand is not None else []
    reason = budget_raise.strip()
    matching = [r for r in added if reason and str(r.get("reason", "")).strip() == reason]
    if matching:
        bounded: list[str] = []
        measured = figures(record)
        for entry in matching:
            start = entry.get("from")
            if isinstance(start, bool) or not isinstance(start, int | float) or main is None:
                continue
            for figure, key in FIGURES:
                if start != main.limits[key]:
                    continue
                try:
                    ceiling = _number(entry.get("to"), f"[[raise]] {reason!r} to")
                except BudgetError as exc:
                    return [str(exc)]
                value = measured[figure]
                if value is not None and value > ceiling:
                    bounded.append(
                        f"suite.{figure} {_fmt(value)} exceeds [[raise]] {reason!r} "
                        f"to {_fmt(ceiling)} by {_fmt(value - ceiling)}"
                    )
        return bounded
    return [*problems, f"budget_raise {budget_raise!r} matches no [[raise]] entry the candidate adds"]
