"""Give manager sessions stable, addressable feature names through sessionTitle on SessionStart and
UserPromptSubmit. A mapping made mid-turn takes effect on the next prompt.

Preserve owner-chosen names. Claude's nameSource distinguishes auto titles; rename only unnamed,
auto-named or previously harness-named sessions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import sessions
from ..records.narrow import as_dict, as_dict_or_none, as_int, dig

TITLE_PREFIX = "mgr"
UNMAPPED = "unmapped"
SHORT_ID_CHARS = 6
# `auto` is a generated title and `derived` one made from the working directory (`agent-ba`,
# read on 2.1.274). Neither was chosen by the owner, so both give way to the harness name.
OVERRIDABLE_SOURCES = (None, "", "auto", "derived")


def desired_title(
    record: dict[str, Any] | None, session_id: str | None = None, *, taken: set[str] | None = None
) -> str | None:
    """Name mapped managers mgr-NNN-slug and unmapped managers mgr-unmapped-<short id>. Workers keep
    their dispatch names. If another live manager owns the base name, add this session's short id
    and retain that suffix to keep peer addressing unambiguous.
    """
    if record and record.get("dispatch_id"):
        return None
    sid = str((record or {}).get("session_id") or session_id or "")
    feature = as_dict((record or {}).get("feature"))
    if feature.get("id") is not None and feature.get("slug"):
        base = f"{TITLE_PREFIX}-{as_int(feature['id']):03d}-{feature['slug']}"
        suffixed = f"{base}-{sid[:SHORT_ID_CHARS]}"
        if taken is not None and sid and ((record or {}).get("name") == suffixed or base in taken):
            return suffixed
        return base
    return f"{TITLE_PREFIX}-{UNMAPPED}-{sid[:SHORT_ID_CHARS]}" if sid else None


def taken_names(root: Path, session_id: str) -> set[str]:
    """Names held by other live manager sessions, for `desired_title`."""
    from ..core import is_alive
    from ..session_liveness import record_live

    out: set[str] = set()
    for record in sessions.all_records(root):
        if record.get("session_id") == session_id or record.get("dispatch_id") or not record.get("name"):
            continue
        if record_live(record, is_alive) and dig(record, "activity", "state") != "ended":
            out.add(str(record["name"]))
    return out


def claude_sessions_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "sessions"


def live_name(pid: int | None) -> tuple[str | None, str | None]:
    """(name, nameSource) of the live Claude session under `pid`, or (None, None)."""
    if not pid:
        return None, None
    try:
        data = as_dict_or_none(
            json.loads((claude_sessions_dir() / f"{pid}.json").read_text(encoding="utf-8"))
        )
    except OSError, ValueError:
        return None, None
    if data is None:
        return None, None
    name, source = data.get("name"), data.get("nameSource")
    return (str(name) if name else None), (str(source) if source else None)


def title_for(root: Path, payload: dict[str, Any]) -> str | None:
    """The title to set now, or None: not mapped, already named so, or named by the owner."""
    if payload.get("agent_id") or payload.get("agent_type"):
        return None
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return None
    record = sessions.get(root, session_id)
    title = desired_title(record, session_id, taken=taken_names(root, session_id))
    if title is None:
        return None
    name, source = live_name(sessions.resolve_pid())
    if name is None:
        name = str(payload.get("session_title") or "") or None
    if name == title:
        if (record or {}).get("name") != title:
            # Claude Code already carries the title but the record lost it. Hooks on one event
            # read and rewrite the record in parallel, and one wrote back the copy it read before
            # this hook set `name`. With no name the tab relabel skipped the session, so a
            # permission prompt showed no ✋. Heal it; Claude needs no new title.
            _set_name(root, session_id, title)
        return None
    ours = name is not None and (
        (name == (record or {}).get("name") and source == "hook")
        or name == (record or {}).get("pending_client_name")
    )
    if name is not None and source not in OVERRIDABLE_SOURCES and not ours:
        return None
    _set_name(root, session_id, title)
    if (record or {}).get("pending_client_name"):
        sessions.set_field(root, session_id, "pending_client_name", None)
    return title


def _set_name(root: Path, session_id: str, title: str) -> None:
    try:
        sessions.set_record(root, session_id, name=title)
    except Exception:  # noqa: BLE001, S110 — naming is a courtesy; the hook never raises
        pass


def session_name(root: Path, payload: dict[str, Any]) -> str:
    """UserPromptSubmit: title a session that was mapped since it started."""
    title = title_for(root, payload)
    if not title:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": title}})
