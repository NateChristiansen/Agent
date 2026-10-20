# ruff: noqa
from __future__ import annotations
from datetime import date
from pathlib import Path
from ..core import atomic_write

JOURNAL_DIR = "journal"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def note_rel(day: date) -> str:
    month = f"{day.month:02d}-{MONTHS[day.month - 1]}"
    return f"{JOURNAL_DIR}/{day.year:04d}/{month}/{day.day:02d}-{WEEKDAYS[day.weekday()]}.md"


def note_path(root: Path, day: date) -> Path:
    return root / note_rel(day)


def ensure_note(root: Path, day: date) -> Path:
    from .block import skeleton

    path = note_path(root, day)
    if not path.exists():
        atomic_write(path, skeleton(day))
    return path
