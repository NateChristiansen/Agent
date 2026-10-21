"""Decide whether an attempt preserves history or restarts with the full work order. Prompt trimming
is safe only for observed Claude SDK sessions or a recorded Codex thread id. Every other attempt
starts with the full contract.
"""

from __future__ import annotations

from typing import Any

from ..records.narrow import as_dict, dict_list


def flood_retried(d: dict[str, Any]) -> bool:
    return any(row.get("reason") == "degenerate_output" for row in dict_list(d.get("auto_resumes")))


def resumes(d: dict[str, Any]) -> bool:
    if int(d["attempt"]) <= 1:
        return False
    if (
        d.get("provider") == "codex"
        and as_dict(d.get("last_result")).get("terminal_reason") == "degenerate_output"
    ):
        return False
    previous_session = d.get("session_established")
    if previous_session is None:
        # Old records predate the explicit marker. A result proves there was a CLI turn;
        # without one, restarting is safer than resuming an unobserved session.
        previous_session = d.get("last_result") is not None
    if not previous_session:
        return False
    return str(d.get("provider") or "claude") != "codex" or bool(d.get("codex_thread_id"))
