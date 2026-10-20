# ruff: noqa
from __future__ import annotations
import os
from pathlib import Path
from .check import Check, new_check


def check_project_instructions(root: Path) -> list[Check]:
    if not (root / "AGENTS.md").exists():
        return [new_check("project instructions", "fail", "AGENTS.md is missing; no context loads")]
    home = Path.home()
    found: list[str] = []
    for directory in [root, *root.parents]:
        for name in ("CLAUDE.md", "CLAUDE.local.md", Path(".claude") / "CLAUDE.md"):
            candidate = directory / name
            if candidate.exists() and candidate != home / ".claude" / "CLAUDE.md":
                found.append(str(candidate))
    if found:
        return [
            new_check(
                "project instructions",
                "fail",
                f"{', '.join(found)} suppresses AGENTS.md on Claude Code; remove it (ADR-0024)",
            )
        ]
    return [
        new_check("project instructions", "ok", "AGENTS.md loads; no CLAUDE.md at or above the root"),
        _nested_instructions(root),
    ]


NESTED_SKIP = {".git", ".venv", "node_modules", "vendor", "__pycache__"}


def _nested_instructions(root: Path) -> Check:
    found: list[str] = []
    seen: set[str] = set()
    for directory, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(directory)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = [d for d in dirs if d not in NESTED_SKIP]
        found += [Path(directory, f).relative_to(root).as_posix() for f in files if f.lower() == "claude.md"]
        if len(found) >= 10:
            break
    if found:
        return new_check(
            "nested instructions",
            "warn",
            f"{', '.join(sorted(found))} loads as Claude instructions when a session reads beside it; rename it",
        )
    return new_check("nested instructions", "ok", "no claude.md below the root")
