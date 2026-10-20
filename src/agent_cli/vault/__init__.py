"""Obsidian-facing generated files: Bases dashboards and the vault home notes.

Organized into `bases` (the five Bases YAML templates and their paths), `dashboard`
(`render_dashboard`, `render_index`, `_today_link`), and `render` (`GENERATED`, `render`,
which writes all of it via `write_if_changed`).

Every name a caller outside this package reach on `vault.<name>` is re-exported below,
so `from . import vault` keeps working unchanged: `status.py` calls `vault.render`;
`tests/test_status.py` reads `vault.GENERATED`, `vault.INDEX` and `vault.DOCS_BASE`, only by
attribute, never monkeypatched."""

from __future__ import annotations

from .bases import ADRS_BASE, BASES_DIR, DOCS_BASE, FEATURES_BASE, HANDOFFS_BASE, JOURNAL_BASE
from .dashboard import render_dashboard, render_index
from .render import DASHBOARD, GENERATED, INDEX, render

__all__ = [
    "ADRS_BASE",
    "BASES_DIR",
    "DASHBOARD",
    "DOCS_BASE",
    "FEATURES_BASE",
    "GENERATED",
    "HANDOFFS_BASE",
    "INDEX",
    "JOURNAL_BASE",
    "render",
    "render_dashboard",
    "render_index",
]
