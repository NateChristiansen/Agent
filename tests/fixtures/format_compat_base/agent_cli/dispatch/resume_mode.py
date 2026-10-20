# ruff: noqa
from __future__ import annotations
from typing import Any


def resumes(d: dict[str, Any]) -> bool:
    if int(d["attempt"]) <= 1:
        return False
    previous_session = d.get("session_established")
    if previous_session is None:
        previous_session = d.get("last_result") is not None
    if not previous_session:
        return False
    return str(d.get("provider") or "claude") != "codex" or bool(d.get("codex_thread_id"))
