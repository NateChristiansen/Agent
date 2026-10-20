# ruff: noqa
from __future__ import annotations
from datetime import date
from pathlib import Path
from typing import Any
from .. import frontmatter
from ..core import AgentError
from ..journal.paths import MONTHS, WEEKDAYS
from ..notes import read_notes_root

USAGE_DIR = "usage"


def base(root: Path) -> Path:
    return read_notes_root(root) or root


def record_rel(day: date, tag: str) -> str:
    month = f"{day.month:02d}-{MONTHS[day.month - 1]}"
    return f"{USAGE_DIR}/{day.year:04d}/{month}/{day.day:02d}-{WEEKDAYS[day.weekday()]}.{tag}.md"


def checkpoint_rel(tag: str) -> str:
    return f"{USAGE_DIR}/machines/{tag}.md"


def render(meta: dict[str, Any]) -> str:
    return frontmatter.join(meta, "")


def read_meta(path: Path) -> dict[str, Any] | None:
    try:
        meta, _ = frontmatter.load(path)
    except AgentError, OSError:
        return None
    return meta


def own_records(where: Path, tag: str) -> dict[date, dict[str, Any]]:
    out: dict[date, dict[str, Any]] = {}
    for path in sorted((where / USAGE_DIR).glob(f"*/*/*.{tag}.md")):
        meta = read_meta(path)
        if meta is None or meta.get("machine") != tag:
            continue
        try:
            day = date.fromisoformat(str(meta.get("date")))
        except ValueError:
            continue
        if where / record_rel(day, tag) == path:
            out[day] = meta
    return out


def own_checkpoint(where: Path, tag: str) -> dict[str, Any] | None:
    meta = read_meta(where / checkpoint_rel(tag))
    return meta if meta is not None and meta.get("machine") == tag else None
