# ruff: noqa
from __future__ import annotations
import getpass
from pathlib import Path
from typing import Any
from ..core import AgentError, iso, now
from ..records.dispatch import git_of, result_of
from ..records.narrow import as_dict, as_dict_or_none, dig
from . import notices
from .state import CLOSED_STATES, is_actioned, is_probe, load, save

FIELD = "verdict_read"
VERDICT_STATES = notices.TERMINAL_NOTICE_STATES


def key(d: dict[str, Any]) -> str:
    return f"{d.get('attempt')}:{notices.generation(d)}"


def has_verdict(d: dict[str, Any]) -> bool:
    return d.get("state") in VERDICT_STATES and (not is_probe(d)) and (d.get("state") not in CLOSED_STATES)


def is_read(d: dict[str, Any]) -> bool:
    if not has_verdict(d) or is_actioned(d):
        return True
    if FIELD not in d:
        return not d.get("chat_pending")
    return dig(d, FIELD, "key") == key(d)


def mark(d: dict[str, Any], by: str, via: str) -> dict[str, Any]:
    entry = {
        "key": key(d),
        "attempt": d.get("attempt"),
        "generation": notices.generation(d),
        "by": by,
        "via": via,
        "at": iso(now()),
    }
    d[FIELD] = entry
    return entry


def has_diff(d: dict[str, Any]) -> bool:
    if d.get("writes") == [] or dig(d, "no_diff", "attempt") == d.get("attempt"):
        return False
    git = git_of(d)
    if git is not None:
        return bool(git["commits"] or git["files"])
    result = result_of(d)
    return bool(result.get("commits") or result.get("files_touched"))


def unresolved_branch(d: dict[str, Any]) -> bool:
    return d.get("state") in ("done", "failed") and (not is_probe(d)) and has_diff(d) and (not is_actioned(d))


def landed_by(session: str | None) -> str:
    if session:
        return session
    try:
        user = getpass.getuser()
    except KeyError, OSError:
        user = "unknown"
    return f"user:{user}"


def live_managers(root: Path) -> dict[str, dict[str, Any]]:
    from ..vault.managers import live_manager_records

    return {str(record["session_id"]): record for record, _ in live_manager_records(root)}


def _feature_of(record: dict[str, Any]) -> int | None:
    value = dig(record, "feature", "id")
    return value if isinstance(value, int) else None


def responsible(d: dict[str, Any], live: dict[str, dict[str, Any]]) -> str | None:
    launcher = d.get("launched_by_session")
    if isinstance(launcher, str) and launcher in live:
        return launcher
    feature = d.get("feature")
    mapped = [
        (str(dig(record, "activity", "at") or ""), sid)
        for sid, record in live.items()
        if feature is not None and _feature_of(record) == feature
    ]
    return max(mapped)[1] if mapped else None


def authorised(d: dict[str, Any], caller: str | None, live: dict[str, dict[str, Any]]) -> bool:
    if not caller:
        return False
    launcher = d.get("launched_by_session")
    if caller == launcher:
        return True
    if isinstance(launcher, str) and launcher in live:
        return False
    record = as_dict_or_none(live.get(caller))
    return record is not None and d.get("feature") is not None and (_feature_of(record) == d.get("feature"))


def may_clear_pending(root: Path, d: dict[str, Any], caller: str | None) -> bool:
    if FIELD in d:
        return True
    if not caller:
        from .notice_producers import caller_session

        caller = caller_session(root)
    return authorised(d, caller, live_managers(root))


def record_read(
    root: Path,
    dispatch_id: str,
    caller: str | None,
    via: str,
    *,
    live: dict[str, dict[str, Any]] | None = None,
    strict: bool = False,
    expected: str | None = None,
) -> dict[str, Any] | None:
    from .verification_gate import record_guard

    d = load(root, dispatch_id)
    if expected is not None and key(d) != expected:
        return None
    if not has_verdict(d):
        if strict:
            raise AgentError(f"dispatch {dispatch_id} is {d.get('state')}; it has no verdict to read")
        return None
    live = live_managers(root) if live is None else live
    if not authorised(d, caller, live):
        if strict:
            raise AgentError(
                f"dispatch {dispatch_id} was launched by another manager"
                + (" that is still live" if d.get("launched_by_session") in live else "")
                + "; only its launcher, or a live manager mapped to its feature once the launcher has ended, marks its verdict read. Nothing was changed."
            )
        return None
    with record_guard(root, dispatch_id) as current:
        if not has_verdict(current) or (expected is not None and key(current) != expected):
            return None
        if dig(current, FIELD, "key") == key(current):
            return as_dict(current[FIELD])
        entry = mark(current, str(caller), via)
        save(root, current)
    return entry
