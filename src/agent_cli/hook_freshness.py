"""Which live managers run hook wiring older than their client's hook config.

A running Claude or Codex client keeps the hook wiring it loaded at start, while the hook code
itself runs current. A live check of a wiring change therefore needs a session started after the
change: a live check run in a Codex session started before the change failed. The live-sessions list marks such a session so nobody tests a wiring change in it.

Measured against the last commit touching the session's own client's file, from the session
record's `started_at`. A resumed session (`claude --resume`) reloads its wiring but keeps its
first `started_at`, so it can read stale when it is not; restarting fresh clears the mark.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

HOOK_CONFIG = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json"}


def changed_at(root: Path, provider: str) -> datetime | None:
    """When the last commit touching the provider's hook config landed, or None."""
    path = HOOK_CONFIG.get(provider)
    if not path:
        return None
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", path],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def stale_since(root: Path, record: dict[str, Any], cache: dict[str, datetime | None]) -> datetime | None:
    """The config change this session predates, or None when its wiring is current or unknown."""
    provider = str(record.get("provider") or "claude")
    if provider not in cache:
        cache[provider] = changed_at(root, provider)
    changed = cache[provider]
    try:
        started = datetime.fromisoformat(str(record.get("started_at") or ""))
    except ValueError:
        return None
    if changed is None or started.tzinfo is None:
        return None
    return changed if started < changed else None


def mark(root: Path, record: dict[str, Any], cache: dict[str, datetime | None]) -> str:
    """A short suffix for a live-session line: empty, or which config the session predates."""
    changed = stale_since(root, record, cache)
    if changed is None:
        return ""
    provider = str(record.get("provider") or "claude")
    return f"hooks stale (started before the {changed:%m-%d %H:%M} {HOOK_CONFIG[provider]} change)"
