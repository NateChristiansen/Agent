"""A day's numbers for the note, summed from every machine's published usage records.

The expected machines are the checkpoint files under `usage/machines/`.
"""

from __future__ import annotations

import datetime as dt
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..core import AgentError, today
from ..dispatch.ids import TAG_RE
from ..records.narrow import as_dict
from ..usage import aggregate
from ..usage import files as usage_files
from . import tokens as tokens_mod
from .paths import note_path

FIGURES = ("spend_usd", "manager_usd", "dispatches", "session_tokens")


def expected_machines(where: Path) -> list[str]:
    folder = where / usage_files.USAGE_DIR / "machines"
    return sorted(p.stem for p in folder.glob("*.md") if TAG_RE.match(p.stem))


def view(root: Path, day: date) -> dict[str, Any]:
    where = usage_files.base(root)
    expected = expected_machines(where)
    records: dict[str, dict[str, Any]] = {}
    checkpoints: dict[str, dict[str, Any]] = {}
    for tag in expected:
        record_path = where / usage_files.record_rel(day, tag)
        record = usage_files.read_meta(record_path)
        if record is not None:
            records[tag] = record
        elif record_path.exists():
            records[tag] = {}  # unreadable or unparseable: fails validation, so `missing`
        checkpoint = usage_files.read_meta(where / usage_files.checkpoint_rel(tag))
        if checkpoint is not None:
            checkpoints[tag] = checkpoint
    out = aggregate(day, expected, records, checkpoints)
    out["tokens_complete"] = bool(out["usage_machines"]) and out["tokens_complete"]
    return out


def models_summary(models: dict[str, dict[str, int]]) -> str:
    short = tokens_mod.short_model
    names = {
        "requests": "requests",
        "input": "input_tokens",
        "output": "output_tokens",
        "cache_write": "cache_creation_input_tokens",
        "cache_read": "cache_read_input_tokens",
    }
    by_model: dict[str, dict[str, int]] = {}
    for model, row in models.items():
        into = by_model.setdefault(short(model), dict.fromkeys(names.values(), 0))
        for key, field in names.items():
            into[field] += row[key]
    return tokens_mod.summary(by_model)


def has_figures(meta: dict[str, Any]) -> bool:
    return any(meta.get(key) for key in FIGURES)


def numbers_for(root: Path, day: date, meta: dict[str, Any]) -> dict[str, Any]:
    """The keyword arguments `meta_for` takes for this day, and `held`: an old note's figures
    stay, with `usage_held` marking them, until every expected machine is counted. Only the
    status fields change meanwhile."""
    seen = view(root, day)
    status = {"usage_machines": seen["usage_machines"], "tokens_complete": seen["tokens_complete"]}
    complete = seen["tokens_complete"]
    legacy = "usage_machines" not in meta or meta.get("usage_held") is True
    if legacy and has_figures(meta) and not complete:
        return {**status, "held": True}
    probe = seen["probe_spend_usd"]
    return {
        **status,
        "held": False,
        "spend_usd": seen["spend_usd"],
        "spend_complete": seen["spend_complete"] and complete,
        "manager_usd": seen["manager_usd"],
        "manager_complete": seen["manager_complete"] and complete,
        "dispatches": seen["dispatches"],
        "session_tokens": seen["session_tokens"],
        "session_models": models_summary(seen["models"]),
        "probe_spend_usd": probe,
    }


def _stamp(value: object) -> str:
    return value.isoformat() if isinstance(value, dt.datetime) else str(value)


def _shape(machines: object) -> dict[str, tuple[str, str]]:
    return {
        str(tag): (str(as_dict(row).get("status")), _stamp(as_dict(row).get("updated")))
        for tag, row in as_dict(machines).items()
    }


def stale_days(root: Path, since: date | None = None) -> list[date]:
    """Days from `since` through yesterday whose note has a generated block and whose
    `usage_machines` no longer matches the records: a newer `updated`, a new machine or a
    status change. `missing_days` is the days with no block at all."""
    from .block import SINCE_DEFAULT
    from .block import _split_full as split_note  # pyright: ignore[reportPrivateUsage]

    out: list[date] = []
    day = since or SINCE_DEFAULT
    end = today() - timedelta(days=1)
    while day <= end:
        path = note_path(root, day)
        if path.exists():
            try:
                meta, generated, _ = split_note(path.read_text(encoding="utf-8"))
            except AgentError, OSError:
                meta, generated = {}, ""
            current = _shape(view(root, day)["usage_machines"]) if generated.strip() else {}
            if generated.strip() and _shape(meta.get("usage_machines")) != current:
                out.append(day)
        day += timedelta(days=1)
    return out
