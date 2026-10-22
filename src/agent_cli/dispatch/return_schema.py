"""The worker return schema: plain data, with no SDK import.

It lived in `options.py`, which imports `claude_agent_sdk`. `prompt.py` reads it to check a
return, and `prompt` sits under `handoffs`, `evaluate` and `watch`, so every hook that touched
a dispatch record loaded the whole SDK. `options` and `rules`
re-export it, so `options.RETURN_SCHEMA` and `rules.RETURN_SCHEMA` still resolve.
"""

from __future__ import annotations

from typing import Any

from .state import RETURN_STATUSES

# Required by the earlier contract. An attempt launched under it may still be collected
# afterwards, so `evaluate` drops these before validating rather than failing it.
LEGACY_RETURN_FIELDS = ("branch", "commits", "files_touched", "commands")

RETURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # Branch, commits and files come from git and the dispatch record, and verification from the
    # supervisor's own run, so the worker no longer reports them.
    "required": ["nonce", "status", "summary", "open_questions", "next_steps", "notes"],
    "properties": {
        "nonce": {"type": "string"},
        "status": {"type": "string", "enum": list(RETURN_STATUSES)},
        # No maxLength: a cap made a worker spend turns at max context shaving its summary
        # under it. The handoff keeps it whole. Displays trim it.
        "summary": {
            "type": "string",
            "description": "One or two sentences on what was done. Aim for under 400 characters; "
            "a longer summary is kept in full and only trimmed for display, never rejected.",
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        # Optional and never required: no comment needs a justification row. A row
        # the worker sends anyway is shown beside its comment in the handoff; old returns with
        # rows still parse.
        "comments": {
            "type": "array",
            "description": "Optional. A note on a comment you added, shown to the reviewer beside it. "
            "Never required; most returns leave this out.",
            "items": {
                "type": "object",
                "required": ["path", "line", "reason"],
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "end": {"type": "integer"},
                    "reason": {"type": "string"},
                },
            },
        },
        # The gate recomputes governing documents from the diff; this list is not authority.
        "conventions": {
            "type": "array",
            "description": "Optional. A doc and the sentence from it, copied exactly, when quoting one "
            "helps the reviewer see why the change fits it. No row is required. If a doc forbids the "
            "change, return `question` instead.",
            "items": {
                "type": "object",
                "required": ["doc", "quote"],
                "properties": {"doc": {"type": "string"}, "quote": {"type": "string"}},
            },
        },
        # Optional so attempts launched before it still validate. Each row becomes one
        # reconciliation item for the manager.
        "findings": {
            "type": "array",
            "description": "Optional. Actionable findings for the manager to reconcile; an empty "
            "list means none.",
            "items": {
                "type": "object",
                "required": ["severity", "summary"],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocking", "optional", "info"]},
                    "summary": {"type": "string"},
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                },
            },
        },
    },
}
