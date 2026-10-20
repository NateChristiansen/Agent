"""Build live manager rows from client session entries and harness records. Hooks supply activity and
obligations; `vault/live.py` publishes the snapshot that Dashboard renders. Client entries keep
managers visible even when a harness record is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import core, sessions
from ..records.narrow import as_dict, as_dict_or_none, as_int, feature_number
from ..session_liveness import pid_unknown as _pid_unknown
from ..session_liveness import quiet as _quiet

STATE_LABELS = {
    "working": "working",
    "waiting_on_you": "waiting on you",
    "idle": "idle",
}
#: The Doing cell for each `live.attention_for` result. `waiting` and
#: `manager_action` are spelled out per row in `rows`.
ATTENTION_LABELS = {
    "question": "question for you",
    "approval": "waiting on you (approval)",
    "working": "working",
    "waiting": "waiting on background work",
    "idle": "idle",
    "unknown": "unknown",
}


def claude_live_sessions() -> dict[str, dict[str, Any]]:
    """Sessions Claude Code itself lists as running, from `~/.claude/sessions/<pid>.json` (read
    on 2.1.273), keyed by session id, each with a live pid. It is the fallback when a harness
    record's pid is stale, and where `kind` (`interactive`, `bg`) and Claude's own name come from."""
    from ..hooks.naming import claude_sessions_dir

    out: dict[str, dict[str, Any]] = {}
    # Under `core.strict_reads` (the tab fallback's derivation) a folder
    # that cannot be listed or a file that cannot be read raises: a skipped live entry would
    # hide a manager from a view that is trusted as complete. A folder not there is no sessions.
    strict = core.reads_strict()
    try:
        paths = core.json_files(claude_sessions_dir())
    except FileNotFoundError:
        return out
    except OSError:
        if strict:
            raise
        return out
    for path in paths:
        try:
            data = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue  # a session that ended mid-scan
        except OSError, ValueError:
            if strict:
                raise
            continue
        if data is not None and data.get("sessionId") and core.is_alive(as_int(data.get("pid"))):
            out[str(data["sessionId"])] = data
    return out


def _usd(value: Any) -> float | None:
    """A known dollar figure, or None. Missing and malformed cost is not measured zero."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except ValueError, TypeError:
        return None


def _in_vault(cwd: Any, root: Path) -> bool:
    if not cwd:
        return False
    try:
        return Path(str(cwd)).resolve().is_relative_to(root.resolve())
    except OSError, ValueError:
        return False


def _question_open(record: dict[str, Any]) -> bool:
    """A pending `owner_question` raised within `live.VALID_IDLE_S`, the longest a derived idle
    state stays valid. It keeps a quiet unknown-pid session listed while the owner may still
    answer; past it a client that died without a SessionEnd drops out. The age is the
    question's own, so a session with no activity yet still counts."""
    from datetime import datetime

    from . import live

    if not record.get("owner_question_pending"):
        return False
    raised = record.get("owner_question_at")
    try:
        age = (core.now() - datetime.fromisoformat(str(raised))).total_seconds()
    except TypeError, ValueError:
        return False
    return age < live.VALID_IDLE_S


def live_manager_records(
    root: Path, held: list[dict[str, Any]] | None = None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return each live manager's harness record and client entry. Client session entries establish
    visibility even if the harness record is missing; records add feature, activity and name.
    Proven live pids also count when the client supplies no list. ended suppresses an exiting
    process until resume restores idle.
    """
    claude_live = claude_live_sessions()
    records = {str(r["session_id"]): r for r in sessions.all_records(root) if r.get("session_id")}
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    for sid, entry in claude_live.items():
        record = records.get(sid)
        if record is None and not _in_vault(entry.get("cwd"), root):
            continue
        # `_synthetic`: no harness record survived, so nothing proves the session unused
        record = record or {"session_id": sid, "_synthetic": True}
        seen.add(sid)
        if record.get("dispatch_id") or as_dict(record.get("activity")).get("state") == "ended":
            continue
        out.append((record, entry))
    holders = {str(lease.get("session_id")) for lease in held or []}
    for sid, record in records.items():
        if sid in seen or record.get("dispatch_id"):
            continue
        if as_dict(record.get("activity")).get("state") == "ended":
            continue
        if _pid_unknown(record):
            # An unknown pid proves nothing either way. Show the session
            # while it is recent, and after that only while it holds a lease the owner may clear.
            if not _quiet(record) or sid in holders or _question_open(record):
                out.append((record, {}))
            continue
        if core.is_alive(record.get("pid")):
            out.append((record, {}))
    return out


def background_roles(
    root: Path,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    held: list[dict[str, Any]],
    dispatches: list[dict[str, Any]],
) -> dict[str, tuple[str, str | None]]:
    """(role, parent session id) per session: `foreground`, `parked` (under its tab), `bg`, or
    `spare`, from Claude Code's own session entries.

    Claude Code answers a Remote Control message sent to an idle tab by forking the tab into a
    background job. The tab's entry names it (`parkedJobId`), and the job's entry carries the same
    `jobId`, so the fork is shown under its tab, not as a second manager. The daemon also starts
    a never-prompted "spare" next to each job. A bg session with no recorded activity, no lease,
    no dispatch and no pending peer message is treated as one and left out of the default views;
    any of those facts brings it back. Parking is decided first, so a parked fork is never taken
    for a spare. Nothing here is inferred from timing."""
    from .. import peers as peers_mod

    parked_by = {
        str(entry["parkedJobId"]): str(record["session_id"])
        for record, entry in pairs
        if entry.get("kind") != "bg" and entry.get("parkedJobId")
    }
    holders = {str(lease.get("session_id")) for lease in held}
    launchers = {str(d.get("launched_by_session")) for d in dispatches}
    roles: dict[str, tuple[str, str | None]] = {}
    for record, entry in pairs:
        sid = str(record["session_id"])
        if entry.get("kind") != "bg":
            roles[sid] = ("foreground", None)
            continue
        parent = parked_by.get(str(entry.get("jobId") or ""))
        if parent and parent != sid:
            roles[sid] = ("parked", parent)
            continue
        # Keep client-listed sessions visible even without harness records; a missing record is not
        # proof of an unused spare.
        unused = (
            not record.get("_synthetic")
            and "activity" not in record
            and sid not in holders
            and sid not in launchers
            and not peers_mod.peek(root, sid)
        )
        roles[sid] = ("spare", None) if unused else ("bg", None)
    return roles


def rows(
    root: Path,
    *,
    dispatches: list[dict[str, Any]] | None = None,
    closed_features: set[int] | None = None,
    held: list[dict[str, Any]] | None = None,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
    actions: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """One row per live manager session, newest activity first. `vault/live.py` passes the
    records it already loaded, so a heartbeat reads each dispatch and plan once, and the
    `manager_action` items `live.obligations` derived, whose count and ages each row shows."""
    from .. import dispatch as dispatch_mod
    from .. import leases as leases_mod
    from ..hooks.task_waits import wait_count
    from .live import approval_show_after, attention_for, peer_wait_until, running_dispatch

    if dispatches is None:
        dispatches = dispatch_mod.load_all(root)
    if closed_features is None:
        closed_features = dispatch_mod.closed_feature_ids(root)
    if held is None:
        held = leases_mod.list_all(root)
    out: list[dict[str, Any]] = []
    if pairs is None:
        pairs = live_manager_records(root, held)
    roles = background_roles(root, pairs, held, dispatches)
    for record, entry in pairs:
        sid = str(record["session_id"])
        background = entry.get("kind") == "bg"
        feature = as_dict(record.get("feature"))
        activity = as_dict(record.get("activity"))
        mine = sorted(str(le.get("area")) for le in held if le.get("session_id") == sid)
        unverified = not entry and _pid_unknown(record) and _quiet(record)
        launched = [d for d in dispatches if d.get("launched_by_session") == sid]
        running = [str(d["id"]) for d in launched if running_dispatch(d)]
        waiting = [
            str(d["id"])
            for d in launched
            if d.get("state") not in ("queued", "running") and dispatch_mod.is_live(d, closed_features)
        ]
        state = str(activity.get("state") or "idle")
        owed = (actions or {}).get(sid, [])
        # The row's attention is the shared projection the tab and Needs you use, not raw
        # activity: an idle session with a pending owner question read "idle" here while its
        # tab showed ✋. The JS only formats `state` (see ADR-0015).
        background_tasks = wait_count(record)
        attention = attention_for(record, len(running) + background_tasks, owed)
        label = ATTENTION_LABELS.get(attention, STATE_LABELS.get(state, state))
        if attention == "waiting" and running:
            label = "waiting on worker " + ", ".join(running)
        elif attention == "manager_action":
            label = f"idle, owes {len(owed)} result{'s' if len(owed) != 1 else ''}"
        name = record.get("name") or entry.get("name") or sid[:8]
        if background:
            # Include live sessions even when idle so their outstanding ownership remains visible.
            name = f"{name} (bg)"
            label = f"background, {entry.get('status') or state}"
        if unverified:
            label = "unverified (process unknown, quiet 30m+)"
        out.append(
            {
                "session_id": sid,
                "name": name,
                "feature": f"{feature_number(feature['id'])} {feature.get('slug')}"
                if feature.get("id") is not None
                else "—",
                "state": label,
                "activity": state,
                "attention": attention,
                "show_after": approval_show_after(record),
                "before_state": "working",
                "show_until": peer_wait_until(record, len(running)),
                "after_state": "idle",
                "background": background,
                "since": activity.get("at"),
                "cost_usd": _usd(record.get("cost_usd")) if record.get("cost_source") != "unknown" else None,
                "running": running,
                "waiting": waiting,
                # What this manager owes from its finished workers: unread verdicts
                # and unresolved branches, oldest first, each as when its result arrived. The view
                # counts the age from these at display time; a stored age froze.
                "manager_action": len(owed),
                "manager_action_since": [item.get("since") for item in owed],
                "leases": mine,
                "unverified": unverified,
                # The Dashboard's "Clear leases" link ("very easy to clear").
                "clear_leases": unverified and bool(mine),
                "parked": [],
            }
        )
    out.sort(key=lambda r: (str(r.get("feature") or ""), str(r["session_id"])))
    # Fold each parked fork under its tab with its own id, cost and leases, not summed into the
    # tab's, and drop spares from the default view.
    by_sid = {r["session_id"]: r for r in out}
    kept: list[dict[str, Any]] = []
    for row in out:
        role, parent = roles.get(row["session_id"], ("foreground", None))
        if role == "spare":
            continue
        if role == "parked" and parent in by_sid:
            by_sid[parent]["parked"].append(
                {k: row[k] for k in ("session_id", "name", "cost_usd", "leases", "clear_leases")}
            )
            continue
        kept.append(row)
    return kept
