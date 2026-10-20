"""Verdict-read: the second of three facts the obligation model keeps apart.

1. **Delivered.** A hook injected the notice: `hook-consumed` on the notice outbox and the
   recipient's store (`hooks/notice_inbox.py`). It governs only re-injection, and never counts
   as read: a hook can die between that save and its output.
2. **Verdict read.** A manager read the result: `verdict_read` on the dispatch record, written by
   `dispatch consume`, by the legacy `dispatch show` alias for one release, by a launcher's own
   reverify, and by a landing. It is bound to the attempt and the notice generation it read, so a
   resume, whose result is a new generation, starts unread.
3. **Resolved.** The dispatch's own state and branch facts: an answered question, a landed,
   acked, abandoned or cancelled branch. Nothing here infers it from a read.

Who may write a read: the launching manager, or, once that session has ended, a live manager
mapped to the same feature (adoption). Another manager's read changes nothing. A landing from a
shell records the landing user instead (`landed_by`).

A record from before this field existed has no `verdict_read` key at all. It reads as read
while its `chat_pending` is clear, which is what every surface showed for it until now.
`vault/live.py` `obligations` is the one reader that turns these facts into obligations.
"""

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
#: The states that carry a worker's verdict: the same set a terminal notice reports.
VERDICT_STATES = notices.TERMINAL_NOTICE_STATES


def key(d: dict[str, Any]) -> str:
    """The attempt and notice generation a read is bound to."""
    return f"{d.get('attempt')}:{notices.generation(d)}"


def has_verdict(d: dict[str, Any]) -> bool:
    """A finished attempt whose result someone has to read. A probe's return is the result
    and waits on nobody (`is_live`), and a closed dispatch has nothing to read."""
    return d.get("state") in VERDICT_STATES and not is_probe(d) and d.get("state") not in CLOSED_STATES


def is_read(d: dict[str, Any]) -> bool:
    """Whether the current verdict has been read. A dispatch the owner acked, abandoned or
    landed after it finished is read: acting on it is reading it."""
    if not has_verdict(d) or is_actioned(d):
        return True
    if FIELD not in d:
        return not d.get("chat_pending")  # a record from before verdict_read existed
    return dig(d, FIELD, "key") == key(d)


def mark(d: dict[str, Any], by: str, via: str) -> dict[str, Any]:
    """Record the read on `d` in memory; the caller holds the record guard and saves."""
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
    """Whether the dispatch's branch carries work: commits or changed files since its base,
    from the git facts the supervisor recorded. A read-only review (`writes=[]`) and a
    supervisor-observed no-diff attempt have none."""
    if d.get("writes") == [] or dig(d, "no_diff", "attempt") == d.get("attempt"):
        return False
    git = git_of(d)
    if git is not None:
        return bool(git["commits"] or git["files"])
    result = result_of(d)  # a record from before the supervisor kept git facts
    return bool(result.get("commits") or result.get("files_touched"))


def unresolved_branch(d: dict[str, Any]) -> bool:
    """A finished build whose branch nobody has landed, acked or abandoned yet. A landing sets
    `actioned_at` (`closing.mark_landed`), so a landed branch is resolved; a resume that
    finishes again after it is not."""
    return d.get("state") in ("done", "failed") and not is_probe(d) and has_diff(d) and not is_actioned(d)


def landed_by(session: str | None) -> str:
    """The reader a landing records: its session, or for a shell landing the landing user."""
    if session:
        return session
    try:
        user = getpass.getuser()
    except KeyError, OSError:
        user = "unknown"
    return f"user:{user}"


# ------------------------------------------------------------------ who is responsible


def live_managers(root: Path) -> dict[str, dict[str, Any]]:
    """Live manager sessions by id, by the rule the Dashboard's manager rows use."""
    from ..vault.managers import live_manager_records

    return {str(record["session_id"]): record for record, _ in live_manager_records(root)}


def _feature_of(record: dict[str, Any]) -> int | None:
    value = dig(record, "feature", "id")
    return value if isinstance(value, int) else None


def responsible(d: dict[str, Any], live: dict[str, dict[str, Any]]) -> str | None:
    """The manager who owes this dispatch's next step: its launcher while that session lives,
    else the live manager mapped to its feature (the most recently active one), else None,
    which makes it the owner's (orphaned)."""
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
    """The launching manager, or, once it has ended, the live manager adopting the feature."""
    if not caller:
        return False
    launcher = d.get("launched_by_session")
    if caller == launcher:
        return True
    if isinstance(launcher, str) and launcher in live:
        return False  # its launcher is still here: another manager's read is not the read
    record = as_dict_or_none(live.get(caller))
    return record is not None and d.get("feature") is not None and _feature_of(record) == d.get("feature")


def may_clear_pending(root: Path, d: dict[str, Any], caller: str | None) -> bool:
    """Whether a read may clear `chat_pending`. On a record from before `verdict_read`, that
    flag is the read fact (`is_read`), so only a reader allowed to write a read may clear it;
    another manager's or a shell's show leaves the verdict unread. On
    a newer record the flag is delivery bookkeeping only and clears as it always did."""
    if FIELD in d:
        return True
    if not caller:
        from .notice_producers import caller_session

        caller = caller_session(root)
    return authorised(d, caller, live_managers(root))


# ------------------------------------------------------------------ writers


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
    """Write `verdict_read` for the current generation when `caller` may. Returns the entry,
    or None when there is nothing to read or the caller may not; `strict` raises instead.
    `expected` is the `key` of the snapshot the caller was shown: the write happens only while
    the record, under its guard, still holds that attempt and generation, and a newer one is
    left unread."""
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
                + "; only its launcher, or a live manager mapped to its feature once the launcher "
                "has ended, marks its verdict read. Nothing was changed."
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
