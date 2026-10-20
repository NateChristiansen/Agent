"""Where a day's journal note lives, and getting one in place."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..core import atomic_write

JOURNAL_DIR = "journal"
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)  # fixed English, not locale

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def note_rel(day: date) -> str:
    """The nested path for `journal/2026/09-September/14-Monday.md`, relative to the vault root.
    The month folder carries its name (`09` alone read as nothing)."""
    month = f"{day.month:02d}-{MONTHS[day.month - 1]}"
    return f"{JOURNAL_DIR}/{day.year:04d}/{month}/{day.day:02d}-{WEEKDAYS[day.weekday()]}.md"


def note_path(root: Path, day: date) -> Path:
    return root / note_rel(day)


def ensure_note(root: Path, day: date) -> Path:
    """Create `journal/YYYY/MM-Month/DD-Weekday.md` from the skeleton when it does not exist
    yet (parent dirs included). Never touches an existing note."""
    from .block import skeleton  # deferred: block.py imports paths.py too

    path = note_path(root, day)
    if not path.exists():
        atomic_write(path, skeleton(day))
    return path
