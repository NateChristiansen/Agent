"""agent doctor: the few checks that matter, assembled by `run` (`run.py` lists them).

`ops.infra` calls `run` and `has_failures`; a test that wants a check module's internals reaches
it as `doctor_mod.checks_mcp.<name>`."""

from __future__ import annotations

from . import checks_instructions, checks_mcp, checks_notes, checks_repos
from .check import Check, new_check
from .run import has_failures, run

__all__ = [
    "Check",
    "checks_instructions",
    "checks_mcp",
    "checks_notes",
    "checks_repos",
    "has_failures",
    "new_check",
    "run",
]
