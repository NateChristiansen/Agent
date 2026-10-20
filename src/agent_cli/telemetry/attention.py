"""Record attention transitions, their causes, notification attempts and consumption. Distinguish
owner waits from background work and delivery from confirmed reading.

Store ids, sessions, causes and counts only; never prompt or message text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def emit(
    root: Path,
    kind: str,
    *,
    session: str | None = None,
    feature: str | None = None,
    provider: str | None = None,
    **data: Any,
) -> None:
    """Record one event in the main checkout's sink. Never raises: telemetry never changes the
    operation it describes."""
    try:
        from . import recorder
        from .recorder import main_of

        clean = {k: v for k, v in data.items() if v is not None}
        recorder.record(
            main_of(root), kind, session=session or None, feature=feature, provider=provider, data=clean
        )
    except Exception:  # noqa: BLE001, S110 — a lost event is counted by the recorder, never raised
        pass


def feature_ref(value: object) -> str | None:
    """`22` or `"022"` as the sink's three-digit feature id, else None."""
    if isinstance(value, int):
        return f"{value:03d}"
    text = str(value or "")
    return text.zfill(3) if text.isdigit() else None


def cause(payload: dict[str, Any]) -> str:
    """Why an activity state changed: the hook event, with the tool or the kind of prompt."""
    from ..hooks.attention import is_wake, machine_prompt

    event = str(payload.get("hook_event_name") or "")
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "").strip()
        if is_wake(payload):
            from ..ops.peer_wake import DISPATCH_WAKE_TEXT

            return "dispatch_wake" if prompt == DISPATCH_WAKE_TEXT else "peer_wake"
        return "machine_prompt" if machine_prompt(prompt) else "owner_prompt"
    tool = str(payload.get("tool_name") or "")
    return f"{event}:{tool}" if tool else event
