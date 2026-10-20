"""Protect shared instruction loading: a competing CLAUDE.md can hide AGENTS.md without a client
warning. Check both expected paths and filesystem casing.
"""

from __future__ import annotations

import os
from pathlib import Path

from .check import Check, new_check


def check_project_instructions(root: Path) -> list[Check]:
    """AGENTS.md is the only always-loaded file, and a CLAUDE.md would silently disable it.

    Claude Code reads `AGENTS.md` natively from v2.1.277, but a `CLAUDE.md`, `.claude/CLAUDE.md`
    or `CLAUDE.local.md` in the working directory *or any directory above it* makes it read
    `CLAUDE.md` files only and skip every `AGENTS.md`. So one file anywhere up the tree costs
    both clients their shared context, with no error and nothing in the transcript to notice.
    ADR-0024 deleted ours; this is what keeps it deleted, including the case where the file
    appears in a parent directory that has nothing to do with this repo.

    `~/.claude/CLAUDE.md` and a managed policy file do not count — they load alongside
    `AGENTS.md` — so neither is reported.
    """
    if not (root / "AGENTS.md").exists():
        return [new_check("project instructions", "fail", "AGENTS.md is missing; no context loads")]
    home = Path.home()
    found: list[str] = []
    for directory in [root, *root.parents]:
        for name in ("CLAUDE.md", "CLAUDE.local.md", Path(".claude") / "CLAUDE.md"):
            candidate = directory / name
            # ~/.claude/CLAUDE.md is the user's own and keeps loading alongside AGENTS.md
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
    """A `claude.md` below the root, in any letter case, loads into a Claude session that reads
    files beside it. macOS and Windows file systems ignore case, so a scratch report named
    `claude.md` became instructions mid-session. Symlinked directories (the
    notes repo's features/, journal/) are followed, since that is where drafts land."""
    found: list[str] = []
    seen: set[str] = set()
    for directory, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(directory)
        if real in seen:  # a symlink loop, or a linked directory reached twice
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
