# ruff: noqa
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from .. import core, sessions
from ..records.narrow import as_dict, as_str, dict_list, dig

MACHINE_PROMPTS = ("<task-notification>", "<agent-message", "<cross-session-message")
"How the prompts Claude Code delivers on its own begin: a background task's notice, a\nsubagent's hand-back and a cross-session message (raw captures, 2026-10). None is the owner's\nanswer to a question."


def is_wake(payload: dict[str, Any]) -> bool:
    from ..ops.peer_wake import WAKE_TEXTS

    return str(payload.get("prompt") or "").strip() in WAKE_TEXTS.values()


def machine_prompt(prompt: str) -> bool:
    from ..ops.peer_wake import WAKE_TEXTS

    text = prompt.strip()
    return text in WAKE_TEXTS.values() or text.startswith(MACHINE_PROMPTS)


def owner_answered(payload: dict[str, Any]) -> bool:
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return False
    return not machine_prompt(str(payload.get("prompt") or ""))


def question_texts(payload: dict[str, Any]) -> list[str]:
    return [
        text
        for q in dict_list(as_dict(payload.get("tool_input")).get("questions"))
        if (text := as_str(q.get("question")).strip())
    ]


def sync_question_tool(tool: str) -> bool:
    return tool == "AskUserQuestion" or tool.endswith("request_user_input")


def raise_question(root: Path, session_id: str, route: str, summary: str | None = None) -> str:
    import uuid

    existing = sessions.get(root, session_id) or {}
    if (existing.get("owner_question_pending") or existing.get("owner_question_replied")) and (
        (existing.get("owner_question_route") or "manual") != route
        or (summary is not None and (summary.strip() or None) != existing.get("owner_question"))
    ):
        clear_question(root, session_id, "replaced")
    stamp = core.iso(core.now())
    out: dict[str, str] = {}

    def change(record: dict[str, Any]) -> dict[str, Any]:
        pending = (
            record.get("owner_question_pending") or record.get("owner_question_replied")
        ) and record.get("owner_question_id")
        if pending:
            new = {
                **record,
                "owner_question_pending": True,
                "owner_question_replied": False,
                "owner_question_at": record.get("owner_question_at") or stamp,
                "owner_question_route": record.get("owner_question_route") or route,
            }
            if summary is not None:
                new["owner_question"] = summary.strip() or None
        else:
            new = {
                **record,
                "owner_question_pending": True,
                "owner_question_replied": False,
                "owner_question": (summary or "").strip() or None,
                "owner_question_id": str(uuid.uuid4()),
                "owner_question_at": stamp,
                "owner_question_route": route,
            }
        out["id"] = str(new["owner_question_id"])
        return new

    record = sessions.update_record(root, session_id, change)
    from .ask_guard import register

    register(root, session_id, route, summary)
    question_id = out["id"]
    if record.get("owner_question_recorded") != question_id:
        from ..telemetry import questions

        written = questions.raised(
            root, session_id, _feature(record), question_id, str(record.get("owner_question_route"))
        )
        sessions.set_field(root, session_id, "owner_question_recorded", written)
    return question_id


def clear_question(root: Path, session_id: str, reason: str = "answered") -> None:
    ended: dict[str, Any] = {}

    def change(record: dict[str, Any]) -> dict[str, Any]:
        if not (record.get("owner_question_pending") or record.get("owner_question_replied")):
            return record
        ended.update(record)
        return {
            **record,
            "owner_question_pending": False,
            "owner_question_replied": False,
            "owner_question": None,
            "owner_question_id": None,
            "owner_question_at": None,
            "owner_question_route": None,
            "owner_question_cleared_id": record.get("owner_question_id"),
            "owner_question_resolution": reason,
        }

    sessions.update_record(root, session_id, change)
    question_id = as_str(ended.get("owner_question_id"))
    if not question_id or ended.get("owner_question_cleared_id") == question_id:
        return
    open_s: int | None = None
    try:
        first = datetime.fromisoformat(as_str(ended.get("owner_question_at")))
        open_s = max(0, int((core.now() - first).total_seconds()))
    except TypeError, ValueError:
        pass
    from ..telemetry import questions

    questions.cleared(
        root,
        session_id,
        _feature(ended),
        question_id,
        as_str(ended.get("owner_question_route")) or "manual",
        reason,
        open_s,
    )


def _feature(record: dict[str, Any]) -> str | None:
    feature = as_dict(record.get("feature")).get("id")
    return f"{feature:03d}" if type(feature) is int else None


def observe(
    root: Path,
    session_id: str,
    payload: dict[str, Any],
    current: str | None,
    state: str | None,
    *,
    provider: str | None = None,
) -> None:
    from ..telemetry import attention as events

    if payload.get("hook_event_name") == "PermissionRequest":
        events.emit(
            root,
            "permission.request",
            session=session_id,
            provider=provider,
            tool=str(payload.get("tool_name") or "") or None,
        )
    if state is not None and state != current:
        transition = {"from": current or "", "to": state, "cause": events.cause(payload)}
        events.emit(root, "activity.transition", session=session_id, provider=provider, **transition)


HEARTBEAT_S = 60
"How stale `activity.seen_at` may get before an unchanged-state event rewrites it."


def heartbeat(root: Path, session_id: str, record: dict[str, Any]) -> None:
    if _fresh(as_dict(record.get("activity"))):
        return
    stamp = core.iso(core.now())
    state = as_str(dig(record, "activity", "state"))
    wrote: list[bool] = []

    def change(existing: dict[str, Any]) -> dict[str, Any]:
        activity = as_dict(existing.get("activity"))
        if activity.get("state") != state or _fresh(activity):
            return existing
        wrote.append(True)
        return {**existing, "activity": {**activity, "seen_at": stamp}}

    try:
        sessions.update_record(root, session_id, change)
        if wrote:
            from ..vault import live

            live.write_quietly(root)
    except Exception as exc:
        sys.stderr.write(f"note: activity heartbeat failed ({exc})\n")


def _fresh(activity: dict[str, Any]) -> bool:
    seen = as_str(activity.get("seen_at")) or as_str(activity.get("at"))
    if not seen:
        return False
    try:
        age = (core.now() - datetime.fromisoformat(seen)).total_seconds()
    except TypeError, ValueError:
        return False
    return 0 <= age < HEARTBEAT_S
