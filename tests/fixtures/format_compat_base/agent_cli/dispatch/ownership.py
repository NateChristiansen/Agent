# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
from .. import frontmatter
from ..core import AgentError

IDENTITY_KEYS = ("id", "feature", "repo")
MISSING = object()


def _token(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def foreign_reason(path: Path, d: dict[str, Any]) -> str | None:
    if not path.exists():
        return None
    try:
        meta, _ = frontmatter.load(path)
    except (AgentError, OSError, UnicodeDecodeError) as exc:
        detail = exc.message if isinstance(exc, AgentError) else str(exc)
        return f"unreadable frontmatter, not adopted: {detail}"
    ours, theirs = (_token(d.get("session_id")), _token(meta.get("session_id")))
    if not theirs:
        return "handoff has no session_id (legacy or hand-edited), not adopted"
    if not ours:
        return "record has no session_id, so it cannot prove ownership"
    if ours != theirs:
        return f"handoff belongs to session_id {theirs}, this record is {ours} (dispatch ids collided)"
    for key in IDENTITY_KEYS:
        mine, file = (d.get(key), meta.get(key))
        if mine is not None and file is not None and (str(mine) != str(file)):
            return f"handoff {key} {file!r} != record {key} {mine!r}"
    return None
