"""Where one machine's usage files live, and reading back only that machine's own.

Records are `usage/YYYY/MM-Month/DD-Weekday.<tag>.md` and the checkpoint is
`usage/machines/<tag>.md`, in the notes repo when one is linked, else under the vault root. Every
read here globs this tag's names only, so another machine's files are never opened.
"""

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
    """The folder `usage/` sits in: the linked notes repo, else the vault root."""
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
    """This tag's daily records by date. A file whose name or `machine` is not this tag's, or
    whose date does not match its path, is skipped."""
    out: dict[date, dict[str, Any]] = {}
    for path in sorted((where / USAGE_DIR).glob(f"*/*/*.{tag}.md")):
        meta = read_meta(path)
        if meta is None or meta.get("machine") != tag:
            continue
        try:
            day = date.fromisoformat(str(meta.get("date")))
        except ValueError:
            continue
        if (where / record_rel(day, tag)) == path:
            out[day] = meta
    return out


def own_checkpoint(where: Path, tag: str) -> dict[str, Any] | None:
    meta = read_meta(where / checkpoint_rel(tag))
    return meta if meta is not None and meta.get("machine") == tag else None
