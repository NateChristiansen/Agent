# ruff: noqa
from __future__ import annotations
from typing import Any
from .state import RETURN_STATUSES

LEGACY_RETURN_FIELDS = ("branch", "commits", "files_touched", "commands")
RETURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nonce", "status", "summary", "open_questions", "next_steps", "notes"],
    "properties": {
        "nonce": {"type": "string"},
        "status": {"type": "string", "enum": list(RETURN_STATUSES)},
        "summary": {
            "type": "string",
            "description": "One or two sentences on what was done. Aim for under 400 characters; a longer summary is kept in full and only trimmed for display, never rejected.",
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "comments": {
            "type": "array",
            "description": "Optional. A note on a comment you added, shown to the reviewer beside it. Never required; most returns leave this out.",
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
        "conventions": {
            "type": "array",
            "description": "Optional. A doc and the sentence from it, copied exactly, when quoting one helps the reviewer see why the change fits it. No row is required. If a doc forbids the change, return `question` instead.",
            "items": {
                "type": "object",
                "required": ["doc", "quote"],
                "properties": {"doc": {"type": "string"}, "quote": {"type": "string"}},
            },
        },
    },
}
