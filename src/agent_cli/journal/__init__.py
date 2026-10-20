"""journal collect and the note writer: assemble one day's material
across features, raw captures, ADRs, git, and write it into `journal/YYYY/MM-Month/DD-Weekday.md`.

Organized into `paths` (where a note lives, `ensure_note`), `block` (the generated block, its
frontmatter properties and `missing_days`; not named `write` because the package exports a
`write` function), and `collect`
(the day's bundle and its markdown rendering, and the `.agent/` scratch writer).

Every name a caller outside this package reach on `journal.<name>` is re-exported
below, so `from . import journal as journal_mod` keeps working unchanged (`hooks/session_blocks.py`,
`hooks/run.py` and `ops/infra.py` call several; the `tests/test_journal_*.py` files reach names by
attribute, none of them monkeypatched)."""

from __future__ import annotations

from .block import (
    GENERATED_END,
    GENERATED_START,
    first_sentence,
    meta_for,
    missing_days,
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
