"""Block a manager's Stop once while it owes reconciliation items on its feature.

A Stop blocks when this session is responsible for owed items in its mapped feature's ledger
(`reconcile.ledger`). Responsibility follows `verdicts.responsible`: a `dispatch:` or `accept:`
source belongs to its launcher while that session lives; a `subagent:` or `landing:` source to the
session it records (the source entry's, else the land journal's) while that session lives; else,
and for a manual source, to the live manager mapped to the feature. `waiting` items are not owed
and never block.

Bound: one block per marker, the ask-guard logical turn plus a hash of the owed set (source,
generation and item). Spent markers are never evicted within a turn; a full list stops blocking. With
ask-guard switched off its turn does not advance, so the marker falls back to the owed set alone.
A new block needs a new owner prompt or a changed owed set; neither comes from a Stop, so the
guard cannot loop. An earlier member's `_stop_blocked` spends nothing. Workers, subagents,
unmapped sessions and a session with an owner question pending pass. The marker lives on the
session record, under the session lock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .. import sessions
from ..records.narrow import as_dict, as_int, as_list, as_str
from . import toggles
from .ask_guard import STATE as ASK_STATE

STATE = "record_guard"
KEEP = 64  # markers kept on the record; a full list stops blocking
SHOWN = 5
LOCK_S = 2.0


def _worker(payload: dict[str, Any], record: dict[str, Any]) -> bool:
    return bool(
        payload.get("agent_id")
        or payload.get("agent_type")
        or record.get("dispatch_id")
        or os.environ.get("AGENT_DISPATCH_ID")
    )


def _recorded(root: Path, source: str, sessions_by_source: dict[str, str]) -> str | None:
    """The session a subagent or landing source records: its current-G entry's, else, for a
    landing, its journal's. None falls to the mapped manager."""
    from .. import land_journal
    from ..land import main_of

    kind, _, ref = source.partition(":")
    if kind not in ("subagent", "landing"):
        return None
    if source in sessions_by_source:
        return sessions_by_source[source]
    if kind == "landing" and ref:
        with contextlib.suppress(Exception):  # a journal not on this machine records nothing here
            return as_str(land_journal.read(main_of(root), ref).get("session")) or None
    return None


def _owner(
    source: str, feature: int, live: dict[str, dict[str, Any]], root: Path, recorded: str | None
) -> str | None:
    from ..dispatch import state, verdicts

    kind, _, ref = source.partition(":")
    d: dict[str, Any] = {"feature": feature}
    if kind in ("dispatch", "accept") and ref:
        with contextlib.suppress(Exception):  # a record not on this machine falls to the mapped manager
            d = {**d, **as_dict(state.load(root, ref))}
    elif recorded:
        d["launched_by_session"] = recorded
    return verdicts.responsible(d, live)


def _owed(root: Path, session_id: str, record: dict[str, Any], feature: int) -> tuple[str, list[Any]]:
    """This session's owed ledger items and the feature's label."""
    from .. import features as features_mod
    from .. import reconcile
    from ..dispatch import verdicts

    f = features_mod.resolve(root, str(feature))
    folded = reconcile.ledger(root, f)
    owed = folded.owed
    if not owed:
        return f.label, []
    live = verdicts.live_managers(root)
    live.setdefault(session_id, record)  # the session stopping is live, whatever the listing says
    owners: dict[str, str | None] = {}
    mine: list[Any] = []
    for item in owed:
        if item.source not in owners:
            recorded = _recorded(root, item.source, folded.sessions)
            owners[item.source] = _owner(item.source, feature, live, root, recorded)
        if owners[item.source] == session_id:
            mine.append(item)
    return f.label, mine


def _line(item: Any) -> str:
    return f"{item.source} {item.item}" + (f" ({item.reason})" if item.reason else "")


def _digest(feature: int, owed: list[Any]) -> str:
    text = json.dumps([feature, sorted([i.source, i.G, i.item] for i in owed)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _reason(label: str, items: list[Any]) -> str:
    owed = [_line(i) for i in items]
    shown = "; ".join(owed[:SHOWN])
    more = f" and {len(owed) - SHOWN} more" if len(owed) > SHOWN else ""
    return (
        f"record-guard: {label} has {len(owed)} item(s) to reconcile that this session owes: "
        f"{shown}{more}. Settle each with the `reconcile` tool (a task, no-task, or `waiting` with "
        "who it waits on), then end the turn. This blocks once per turn and owed set."
    )


def record_guard(root: Path, payload: dict[str, Any]) -> str:
    if str(payload.get("hook_event_name") or "Stop") != "Stop" or payload.get("_stop_blocked"):
        return ""
    session_id = as_str(payload.get("session_id"))
    if not session_id:
        return ""
    record = sessions.get(root, session_id)
    if not isinstance(record, dict) or _worker(payload, record):  # pyright: ignore[reportUnnecessaryIsInstance]
        return ""
    feature = as_dict(record.get("feature")).get("id")
    if type(feature) is not int or record.get("owner_question_pending"):
        return ""
    label, owed = _owed(root, session_id, record, feature)
    if not owed:
        return ""
    digest = _digest(feature, owed)
    by_turn = not toggles.disabled(root, "ask-guard")
    fired: list[bool] = []

    def change(current: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(current, dict) or current.get("owner_question_pending"):  # pyright: ignore[reportUnnecessaryIsInstance]
            return current
        state = dict(as_dict(current.get(STATE)))
        markers = [as_str(m) for m in as_list(state.get("markers")) if as_str(m)]
        turn = as_int(as_dict(current.get(ASK_STATE)).get("turn")) or 0
        marker = f"{turn}:{digest}"
        if by_turn:
            markers = [m for m in markers if m.startswith(f"{turn}:")]  # an advanced turn never returns
        spent = marker in markers if by_turn else any(m.endswith(f":{digest}") for m in markers)
        if spent or len(markers) >= KEEP:  # a full list passes: evicting a spent marker would rearm it
            return current
        fired.append(True)
        state["markers"] = [*markers, marker]
        return {**current, STATE: state}

    sessions.update_record(root, session_id, change, LOCK_S)
    if not fired:
        return ""
    return json.dumps({"decision": "block", "reason": _reason(label, owed)})
