"""Run harness health checks for configuration, state, processes, instructions and integrations."""

from __future__ import annotations

from pathlib import Path

from . import (
    checks_instructions,
    checks_land,
    checks_mcp,
    checks_notes,
    checks_procs,
    checks_repos,
    checks_state,
    checks_telemetry,
    checks_windows,
)
from .check import Check


def run(root: Path) -> list[Check]:
    checks: list[Check] = []
    checks += checks_repos.check_registry(root)
    checks += checks_notes.check_notes_repo(root)
    checks += checks_notes.check_private_set(root)
    checks += checks_notes.check_notes_sync(root)
    checks += checks_mcp.check_mcp_wiring(root)
    checks += checks_mcp.check_codex_mcp_wiring(root)
    checks += checks_windows.rows(root)
    checks += checks_telemetry.rows(root)
    checks += checks_procs.check_procs(root)
    checks += checks_land.rows(root)
    checks += checks_state.rows(root)
    checks += checks_instructions.check_project_instructions(root)
    return checks


def has_failures(checks: list[Check]) -> bool:
    return any(c["level"] == "fail" for c in checks)
