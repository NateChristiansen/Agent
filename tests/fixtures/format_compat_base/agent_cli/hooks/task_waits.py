# ruff: noqa
from __future__ import annotations
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from .. import core, sessions
from ..records.narrow import as_dict, as_dict_or_none, as_list_or_none, dict_list

TASK_ID_FIELDS = ("backgroundTaskId", "taskId", "agentId")
"What a tool result calls the handle of work that outlives its call. `Bash` with\n`run_in_background` returns the first, `Monitor` the second, and a backgrounded subagent the\nthird. All three close on the same `<task-id>` in the notification that reports them finished."
ASYNC_ONLY_FIELDS = ("agentId",)
"Handles that only mean outstanding work when the call was asynchronous. A subagent that runs\ninline returns in its own tool call and is already finished by the time this hook sees it;\nopening a task for one would never close and would pin the tab to the waiting glyph."
PERMISSION_FIELD = "subagent_permission"
"Subagents of this session waiting on the owner's permission prompt, by `agent_id`. A subagent's\n`PermissionRequest` carries the manager's session_id and its own\n`agent_id`, which is also its background task id. The manager's own activity is left alone, so\nwhen the prompt resolves the tab returns to whatever it showed before. Resolved by that\nsubagent's next `PostToolUse` (allowed), or by the subagent ending (denied, or it gave up):\nits finished notice, or a `Stop` that no longer lists it."


def pending_after(payload: dict[str, Any], pending: Mapping[str, object]) -> dict[str, object] | None:
    event = str(payload.get("hook_event_name") or "")
    if event == "SessionStart":
        source = str(payload.get("source") or payload.get("session_reason") or "")
        if source == "compact":
            return None
        return {} if pending else None
    if event == "PostToolUse":
        response = as_dict_or_none(payload.get("tool_response"))
        if response is None:
            return None
        for field in TASK_ID_FIELDS:
            if field in ASYNC_ONLY_FIELDS and (not response.get("isAsync")):
                continue
            task_id = response.get(field)
            if isinstance(task_id, str) and task_id and (task_id not in pending):
                return {**pending, task_id: core.iso(core.now())}
        return None
    if event == "UserPromptSubmit":
        from .task_notices import finished_ids

        finished = finished_ids(str(payload.get("prompt") or ""))
        remaining = {k: v for k, v in pending.items() if k not in finished}
        return remaining if len(remaining) != len(pending) else None
    return None


def reported_tasks(
    payload: dict[str, Any], pending: Mapping[str, object], previously_reported: Iterable[str] = ()
) -> dict[str, object] | None:
    listed = as_list_or_none(payload.get("background_tasks"))
    if listed is None:
        return None
    ids = [str(task["id"]) for task in dict_list(listed) if task.get("id")]
    gone = set(previously_reported) - set(ids)
    reported = {
        **{task_id: stamp for task_id, stamp in pending.items() if task_id not in gone},
        **{task_id: pending.get(task_id) or core.iso(core.now()) for task_id in ids},
    }
    return reported if reported != pending else None


def wait_count(record: dict[str, Any]) -> int:
    from ..peers import pending_waits

    return (
        len(as_dict(record.get("background_tasks")))
        + len(as_dict(record.get("session_crons")))
        + len(pending_waits(record))
    )


def waits_after(
    event: str, waits: Mapping[str, object], before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, object] | None:
    if event in ("Stop", "SessionStart"):
        kept = {k: v for k, v in waits.items() if k in after}
    else:
        closed = set(before) - set(after)
        kept = {k: v for k, v in waits.items() if k not in closed}
    return kept if kept != waits else None


def subagent_event(root: Path, payload: dict[str, Any]) -> None:
    event = str(payload.get("hook_event_name") or "")
    agent = str(payload.get("agent_id") or "")
    session_id = str(payload.get("session_id") or "")
    if event not in ("PermissionRequest", "PostToolUse") or not agent or (not session_id):
        return
    record = sessions.get(root, session_id)
    if not record or record.get("dispatch_id"):
        return
    waits = dict(record.get(PERMISSION_FIELD) or {})
    if event == "PermissionRequest":
        if agent in waits:
            return
        waits[agent] = core.iso(core.now())
    elif waits.pop(agent, None) is None:
        return
    sessions.set_field(root, session_id, PERMISSION_FIELD, waits)
    from .activity import refresh_tab

    refresh_tab(root, session_id)
