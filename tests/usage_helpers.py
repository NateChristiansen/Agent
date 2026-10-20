"""Sample schema-1 usage records and checkpoints for the usage tests."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

UPDATED = "2026-10-10T09:15:02-06:00"


def sample_record(machine: str = "mac", **overrides: Any) -> dict[str, Any]:
    """A valid record: 1,000 Claude tokens over 2 requests and 500 Codex tokens over 1."""
    record: dict[str, Any] = {
        "tags": ["usage"],
        "schema": 1,
        "date": "2026-10-09",
        "machine": machine,
        "updated": UPDATED,
        "status": "complete",
        "total_tokens": 1500,
        "requests": 3,
        "tokens_manager": 700,
        "tokens_subagent": 100,
        "tokens_worker": 500,
        "tokens_reviewer": 0,
        "tokens_probe": 0,
        "tokens_eval": 0,
        "tokens_drafter": 0,
        "tokens_other": 200,
        "manager_usd": 4.5,
        "manager_complete": True,
        "dispatches": 2,
        "dispatch_usd": 3.25,
        "probe_usd": 0.0,
        "spend_complete": True,
        "models": {
            "claude-opus-5-5": {
                "requests": 2,
                "input": 10,
                "output": 90,
                "cache_write": 100,
                "cache_read": 800,
            },
            "gpt-6-sol": {"requests": 1, "input": 50, "output": 50, "cache_write": 0, "cache_read": 400},
        },
        "sources": {
            "claude": {"status": "complete", "files": 2, "requests": 2},
            "codex": {"status": "complete", "files": 1, "requests": 1},
            "costs": {"status": "complete"},
        },
    }
    record.update(copy.deepcopy(overrides))
    return record


def publish(
    root: Path,
    day: date,
    machine: str = "mac",
    record: bool | dict[str, Any] = True,
    covered_through: str = "2026-12-31T00:00:00-06:00",
) -> None:
    """Write `machine`'s checkpoint, and its record for `day` unless `record` is False. A dict
    `record` overrides the sample's fields; `date` and `machine` are always set."""
    from agent_cli import frontmatter
    from agent_cli.usage import files

    where = files.base(root)
    target = where / files.checkpoint_rel(machine)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.join(sample_checkpoint(machine, covered_through), ""), encoding="utf-8")
    if record is False:
        return
    extra = record if isinstance(record, dict) else {}
    meta = sample_record(machine, **{**extra, "date": day.isoformat()})
    path = where / files.record_rel(day, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.join(meta, ""), encoding="utf-8")


def sample_checkpoint(
    machine: str = "mac", covered_through: str = "2026-10-10T09:00:00-06:00"
) -> dict[str, Any]:
    return {
        "tags": ["usage-checkpoint"],
        "schema": 1,
        "machine": machine,
        "updated": UPDATED,
        "covered_through": covered_through,
        "sources": {
            name: {"covered_through": covered_through, "status": "complete"}
            for name in ("claude", "codex", "costs")
        },
    }
