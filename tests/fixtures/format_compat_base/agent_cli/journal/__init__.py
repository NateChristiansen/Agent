# ruff: noqa
from __future__ import annotations
from .block import (
    GENERATED_END,
    GENERATED_START,
    first_sentence,
    meta_for,
    missing_days,
    refresh_numbers,
    render_note,
    skeleton,
    split_sections,
    write_generated,
)
from .collect import collect, render_markdown, write
from .paths import ensure_note, note_path, note_rel
from .pending import clear as pending_clear
from .pending import read as pending_read
from .pending import record as pending_record
from .pending import summary as pending_summary
from .usage_note import stale_days
from .writer import is_writer, require_writer

__all__ = [
    "GENERATED_END",
    "GENERATED_START",
    "meta_for",
    "collect",
    "ensure_note",
    "first_sentence",
    "is_writer",
    "missing_days",
    "refresh_numbers",
    "require_writer",
    "stale_days",
    "note_path",
    "note_rel",
    "pending_clear",
    "pending_read",
    "pending_record",
    "pending_summary",
    "render_markdown",
    "render_note",
    "skeleton",
    "split_sections",
    "write",
    "write_generated",
]
