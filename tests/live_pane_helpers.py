"""Render snapshots and tick deadlines through the plugin's LivePane under Node."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def view(
    tmp_path: Path,
    section: str | None,
    seconds: int,
    snap: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> list[list[str]]:
    """Draw at `rendered_at`, then tick `seconds` later without another snapshot write."""
    stamp = snap.get("rendered_at") or snap.get("observed_at") or "2026-10-15T00:00:00Z"
    moment = at or datetime.fromisoformat(stamp)
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/fixtures/probe_live_pane.js"),
            str(ROOT / "harness/obsidian/plugins/agent-terminal-focus/main.js"),
            json.dumps(
                {
                    "snapshot": snap,
                    "section": section,
                    "now": moment.timestamp() * 1000,
                    "seconds": seconds,
                    "prior": prior,
                }
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def texts(nodes: list[list[str]]) -> str:
    return " | ".join(str(n[1]) for n in nodes)
