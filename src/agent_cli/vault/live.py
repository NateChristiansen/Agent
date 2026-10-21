"""Publish `.agent/live.json` from shared Python domain rules; Obsidian renders it without duplicating
decisions (see ADR-0015).

obligations derives owner needs and manager actions from separate delivery, reading and resolution
facts. Tabs and session-start use the same derivation. Every item carries observed_at and
valid_until; expired observations become unknown. show_after delays permission painting while the
fallback rechecks the transition before painting.

Activity/session-start hooks, supervisor attempt boundaries and heartbeats, and status render
refresh this ignored snapshot. The plugin paints tabs from it; frequent writes create no
committed-note churn.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import core
from ..records.narrow import as_dict, as_dict_or_none, as_list, as_list_or_none, dig

LIVE_JSON = core.agent_path(Path(), "live.json")
HEARTBEAT_SECONDS = 5.0
# how far back from the end of `.out` to look for the last tool call: one assistant line is
# small, but a result envelope can be tens of KB
TAIL_BYTES = 64 * 1024


def path(root: Path) -> Path:
    return root / LIVE_JSON


def last_tool(out_path: Path) -> str | None:
    """The last tool the worker reached for, from the newest `assistant` line in `.out` that
    names one (`dispatch/normalize.py:out_record`). Reads only the file's tail."""
    try:
        with out_path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        try:
            record = as_dict(json.loads(line))
        except ValueError:
            continue  # the first line of the tail is usually cut mid-record
        tools = as_list(record.get("tools"))
        if record.get("type") == "assistant" and tools:
            return str(tools[-1])
    return None


def _mtime_iso(p: Path) -> str | None:
    try:
        return core.iso(datetime.fromtimestamp(p.stat().st_mtime).astimezone())
    except OSError:
        return None


def _worker(root: Path, d: dict[str, Any], features_by_id: dict[int, Any]) -> dict[str, Any]:
    from ..dispatch import handoffs, opener, prompt, state

    running = d.get("state") in ("queued", "running")
    out = state.file(root, str(d["id"]), ".out")
    feature = features_by_id.get(d.get("feature"))  # type: ignore[arg-type]
    return {
        "id": d["id"],
        "feature": d.get("feature"),
        "feature_slug": d.get("feature_slug") or (feature.slug if feature else None),
        "repo": d.get("short"),
        "state": d.get("state"),
        "attempt": d.get("attempt"),
        "max_attempts": d.get("max_attempts"),
        "model": d.get("model"),
        "effort": d.get("effort"),
        "started": d.get("started"),
        "attempt_started": d.get("attempt_started"),
        "finished": d.get("finished"),
        "cost_usd": round(float(d.get("cost_usd") or 0.0), 2),
        # tokens while it runs, dollars once it has returned
        "tokens": int(d.get("tokens") or 0),
        "goal": state.display_summary(state.first_line(prompt.manager_prompt(d)), handoffs.ASK_CHARS),
        "ask": handoffs.handoff_ask(d),
        "last_activity": _mtime_iso(out) if running else None,
        "last_tool": last_tool(out) if running else None,
        "supervisor_alive": core.is_alive(d.get("supervisor_pid")) if running else None,
        "launched_by_session": d.get("launched_by_session"),
        "handoff": d.get("handoff"),
        "open": opener.open_uri(root, str(d["id"])),
    }


# ------------------------------------------------------------------ obligations

#: How long a derivation stands. While a worker runs, the supervisor's heartbeat re-derives every
#: `HEARTBEAT_SECONDS`, so three missed beats make it unknown. Otherwise only events change the
#: facts, and every hook event that changes a session's state re-derives, so a quiet idle
#: harness stays true for long: 12 h, so idle tabs do not all read `✅?` after a coffee break,
#: while a snapshot left over from a stopped machine still goes unknown by the next day.
VALID_RUNNING_S = 3 * HEARTBEAT_SECONDS
VALID_IDLE_S = 12 * 60 * 60
#: Item fields that change with the clock alone, left out when deciding whether to rewrite.
#: No age is stored: an item carries `since`, when its result arrived, and every surface counts
#: the age from it at display time. A stored age froze at its first value, because a rewrite
#: that only the clock would change is skipped.
VOLATILE = frozenset({"observed_at", "valid_until"})


def age_s(since: Any, moment: datetime | None = None) -> int | None:
    """Seconds from `since` (an ISO stamp) to `moment`, now by default, or None if unreadable."""
    try:
        return max(0, int(((moment or core.now()) - datetime.fromisoformat(str(since))).total_seconds()))
    except TypeError, ValueError:
        return None


def _earliest(stamps: list[Any]) -> str | None:
    """The earliest readable ISO stamp, compared as instants."""
    parsed: list[tuple[datetime, str]] = []
    for stamp in stamps:
        try:
            parsed.append((datetime.fromisoformat(str(stamp)), str(stamp)))
        except TypeError, ValueError:
            continue
    try:
        return min(parsed)[1] if parsed else None
    except TypeError:  # a naive stamp beside aware ones
        return None


def _resume_action(root: Path, items: list[dict[str, Any]]) -> dict[str, Any]:
    """How the owner gets a manager back on an orphaned feature: resume the session that last
    launched its work, in the vault root, the way the ribbon starts a manager there (the client
    command in a vault-root terminal). With no known session, a new ribbon manager adopts the
    feature with `feature use`."""
    from .. import sessions

    for item in sorted(items, key=lambda i: str(i.get("since") or ""), reverse=True):
        sid = item.get("launched_by_session")
        if not isinstance(sid, str) or not sid:
            continue
        record = sessions.get(root, sid) or {}
        client = str(record.get("provider") or "claude")
        command = f"codex resume {sid}" if client == "codex" else f"claude --resume {sid}"
        return {"session_id": sid, "client": client, "cwd": str(root), "command": command}
    return {"session_id": None, "client": None, "cwd": str(root), "command": None}


def dispatch_obligation(d: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    """Classify one dispatch as an owner question/block or manager work (unread verdict, unresolved
    branch or incomplete cleanup). Orphaned manager work goes to the owner. obligations suppresses
    closed features using feature state; dispatch.state.save watches relevant record changes.
    """
    from ..dispatch import verdicts
    from ..dispatch.state import CLOSED_STATES, is_actioned, is_probe

    state = d.get("state")
    if is_probe(d) or state in ("queued", "running"):
        return None
    cleanup = bool(d.get("verification_cleanup_pending"))
    if (state in CLOSED_STATES or is_actioned(d)) and not cleanup:
        return None
    if state in ("question", "blocked") and not is_actioned(d):
        return ("owner", (str(state),))
    reasons = tuple(
        reason
        for reason, owed in (
            ("cleanup", cleanup),
            ("unread", not verdicts.is_read(d)),
            ("unresolved-branch", verdicts.unresolved_branch(d)),
        )
        if owed
    )
    return ("manager", reasons) if reasons else None


def obligations(
    root: Path,
    *,
    dispatches: list[dict[str, Any]] | None = None,
    closed: set[int] | None = None,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
    moment: datetime | None = None,
) -> dict[str, Any]:
    """The one derivation of what is owed, and to whom (`dispatch/verdicts.py` names the three facts it reads). It feeds `live.json`, the tab glyph and the session-start
    block, and nothing else derives these.

    - `needs_you`, the owner's: a manager's pending question (tool or `owner_question`) or
      approval; a worker in `question` or `blocked`, read or not; and orphaned work, an unread
      verdict or unresolved branch with no live responsible manager, as one row per feature
      whose action is "resume feature manager".
    - `manager_action`, by session: unread verdicts, unresolved build branches and incomplete
      cleanups whose launcher is live, or, once it has ended, the live manager mapped to the
      feature. Each item carries `since`, when its result arrived, from which every surface
      counts its age at display time (`age_s`); oldest first.

    Closed features and probes owe nothing, as in `is_live`. Every item carries `observed_at`,
    `valid_until` and `show_after` (Codex's permission debounce)."""
    from .. import dispatch as dispatch_mod
    from ..dispatch import handoffs, opener, verdicts
    from ..hooks.task_waits import PERMISSION_FIELD
    from . import managers as managers_mod

    if dispatches is None:
        dispatches = dispatch_mod.load_all(root)
    if closed is None:
        closed = dispatch_mod.closed_feature_ids(root)
    if pairs is None:
        pairs = managers_mod.live_manager_records(root)
    live = {str(record["session_id"]): record for record, _ in pairs}
    observed = moment or core.now()
    running_any = any(running_dispatch(d) for d in dispatches)
    until = observed.timestamp() + (VALID_RUNNING_S if running_any else VALID_IDLE_S)
    stamp = {
        "observed_at": core.iso(observed),
        "valid_until": core.iso(datetime.fromtimestamp(until).astimezone(observed.tzinfo)),
        "show_after": None,
    }

    questions: list[dict[str, Any]] = []
    for record, entry in pairs:
        sid = str(record["session_id"])
        activity = as_dict(record.get("activity"))
        name = record.get("name") or entry.get("name") or sid[:8]
        head = {
            "kind": "manager",
            "session_id": sid,
            "name": name,
            "feature": as_dict(record.get("feature")).get("id"),
        }
        if record.get("owner_question_pending"):
            since = record.get("owner_question_at") or activity.get("at")
            questions.append(
                {**head, "reason": "question", "text": record.get("owner_question"), "since": since, **stamp}
            )
        elif activity.get("state") == "waiting_on_you" or as_dict(record.get(PERMISSION_FIELD)):
            questions.append(
                {
                    **head,
                    "reason": "approval",
                    "since": activity.get("at"),
                    **stamp,
                    "show_after": approval_show_after(record),
                }
            )

    workers: list[dict[str, Any]] = []
    actions: dict[str, list[dict[str, Any]]] = {}
    orphans: dict[Any, list[dict[str, Any]]] = {}
    for d in dispatches:
        if d.get("feature") in closed:
            continue
        owed = dispatch_obligation(d)
        if owed is None:
            continue
        state = d.get("state")
        item = {
            "kind": "worker",
            "id": d["id"],
            "feature": d.get("feature"),
            "state": state,
            "ask": handoffs.handoff_ask(d),
            "open": opener.open_uri(root, str(d["id"])),
            "since": d.get("finished"),  # when the result arrived; the age is counted from it
            "read": verdicts.is_read(d),
            "launched_by_session": d.get("launched_by_session"),
            **stamp,
        }
        if owed[0] == "owner":
            workers.append(item)  # the owner's, read or not, until answered or resumed
            continue
        item["reasons"] = list(owed[1])
        owner = verdicts.responsible(d, live)
        if owner is not None:
            actions.setdefault(owner, []).append(item)
        else:
            orphans.setdefault(d.get("feature"), []).append(item)

    rows: list[dict[str, Any]] = []
    for feature, items in sorted(orphans.items(), key=lambda kv: str(kv[0])):
        rows.append(
            {
                "kind": "orphaned",
                "feature": feature,
                "count": len(items),
                "ids": [i["id"] for i in items],
                "oldest_since": _earliest([i.get("since") for i in items]),
                "action": "resume feature manager",
                "resume": _resume_action(root, items),
                **stamp,
            }
        )
    for owed in actions.values():
        owed.sort(key=lambda i: str(i.get("since") or ""))
    return {
        "observed_at": stamp["observed_at"],
        "valid_until": stamp["valid_until"],
        "needs_you": questions + workers + rows,
        "manager_action": actions,
    }


def attention_for(
    record: dict[str, Any], running: int, owed: list[dict[str, Any]], *, valid: bool = True
) -> str:
    """The one attention projection a surface shows for a manager session: `question`,
    `approval`, `working`, `waiting` (one of its workers or background tasks runs),
    `manager_action` (it owes finished results), `idle`, or `unknown` once the data is stale."""
    from ..hooks.task_waits import PERMISSION_FIELD

    if not valid:
        return "unknown"
    state = dig(record, "activity", "state")
    if record.get("owner_question_pending"):
        return "question"
    if state == "waiting_on_you" or as_dict(record.get(PERMISSION_FIELD)):
        return "approval"
    if state == "working":
        return "working"
    if running:
        return "waiting"
    return "manager_action" if owed else "idle"


OWNER_ATTENTION = ("question", "approval")
"""The `attention_for` projections that are the owner's to act on: the two a manager is listed
under in `needs_you` (`build`), and the ✋ on its tab."""


def running_dispatch(d: dict[str, Any]) -> bool:
    """Only a live supervisor can keep a worker waiting or renew the short validity window."""
    return d.get("state") in ("queued", "running") and core.is_alive(d.get("supervisor_pid"))


def approval_show_after(record: dict[str, Any]) -> str | None:
    """Codex permission deadline. Owner questions and subagent prompts remain immediate."""
    from ..hooks.task_waits import PERMISSION_FIELD

    if (
        record.get("provider") != "codex"
        or record.get("owner_question_pending")
        or record.get(PERMISSION_FIELD)
    ):
        return None
    activity = as_dict(record.get("activity"))
    return (
        str(activity["show_after"])
        if activity.get("state") == "waiting_on_you" and activity.get("show_after")
        else None
    )


def tab_timing(record: dict[str, Any], label: str) -> dict[str, Any]:
    """Clock-only presentation fields; both painters copy the supplied labels."""
    from .. import obsidian_tabs

    after = approval_show_after(record)
    return (
        {
            "show_after": after,
            "before_label": obsidian_tabs.label_for(obsidian_tabs.label_name(label), "working", []),
        }
        if after
        else {}
    )


def peer_wait_until(record: dict[str, Any], running: int) -> str | None:
    """A quiet manager waiting only on peers becomes idle at its last request's timeout."""
    from ..hooks.task_waits import PERMISSION_FIELD
    from ..peers import pending_waits

    if (
        running
        or dig(record, "activity", "state") != "idle"
        or any(
            record.get(k)
            for k in (
                "background_tasks",
                "session_crons",
                "owner_question_pending",
                PERMISSION_FIELD,
            )
        )
    ):
        return None
    waits = pending_waits(record)
    return max((str(w["until"]) for w in waits.values()), default=None)


def owner_obligation(record: dict[str, Any]) -> str | None:
    """Return question, approval or None for one manager. sessions.update_record watches changes
    through begin_intent(watch=True) so a stalled writer cannot hide an owner obligation.
    """
    reason = attention_for(record, 0, [])
    return reason if reason in OWNER_ATTENTION else None


def tab_entries(
    root: Path,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    dispatches: list[dict[str, Any]],
    live_workers: list[dict[str, Any]],
    owed: dict[str, Any],
) -> list[dict[str, Any]]:
    """What every terminal tab should read, for the agent-terminal-focus plugin to paint.
    One entry per live manager, from `activity.desired_label`, the code
    the hooks' own relabel uses, and one per live worker's watch tab, from
    `obsidian_tabs.worker_label`. Each carries the ready `label` and the `stale_label` a tab
    shows past `valid_until`, plus how to find the tab (`activity.tab_target`), so the plugin
    derives nothing (see ADR-0015). `key` is the drainer's key for the same tab.

    A worker that has just left the live set (cancelled, acked, closed) keeps its watch entry,
    with its final label, until the plugin's heartbeat shows that label painted or
    `FINAL_PAINT_GRACE_S` has passed. Dropping it at once left
    the tab reading ◐ for good: the plugin had nothing to paint, and its fresh heartbeat held
    the drainer's final relabel back."""
    from .. import obsidian_tabs, tab_plugin
    from ..hooks.activity import desired_label, tab_target
    from ..tab_drain import drain_key

    live_ids = {str(d["id"]) for d in live_workers}
    painted = as_dict(tab_plugin.heartbeat(root).get("painted"))
    now = core.now()
    final: list[dict[str, Any]] = []
    for d in dispatches:
        wid = str(d.get("id") or "")
        if not wid or wid in live_ids or d.get("state") in ("queued", "running"):
            continue
        left = _left_at(root, d)
        if left is None or (now - left).total_seconds() > FINAL_PAINT_GRACE_S:
            continue
        label = obsidian_tabs.worker_label(wid, d.get("state"))
        if painted.get(drain_key("watch", wid)) != label:
            final.append(d)

    stamp = {"observed_at": owed["observed_at"], "valid_until": owed["valid_until"]}
    out: list[dict[str, Any]] = []
    for record, _ in pairs:
        sid = str(record.get("session_id") or "")
        try:
            desired = desired_label(root, sid, record, dispatches=dispatches, owed=owed)
        except Exception as exc:  # noqa: BLE001 — one odd record must not cost every tab
            sys.stderr.write(f"note: tab label for {sid[:8]} failed ({exc})\n")
            continue
        if not sid or desired is None:
            continue
        name, _, label = desired
        peer_until = peer_wait_until(
            record, sum(running_dispatch(d) for d in dispatches if d.get("launched_by_session") == sid)
        )
        out.append(
            {
                "key": drain_key("manager", sid),
                "kind": "manager",
                "provider": record.get("provider"),
                "identity": sid,
                **tab_target(sid, record, name),
                "label": label,
                "stale_label": label
                if record.get("owner_question_pending")
                else obsidian_tabs.stale_label(label),
                "owner_question": bool(record.get("owner_question_pending")),
                "stale_reads": 3,
                **tab_timing(record, label),
                **(
                    {
                        "show_until": peer_until,
                        "after_label": obsidian_tabs.label_for(obsidian_tabs.label_name(label), "idle", []),
                    }
                    if peer_until
                    else {}
                ),
                **stamp,
            }
        )
    for d in [*live_workers, *final]:
        wid = str(d["id"])
        label = obsidian_tabs.worker_label(wid, d.get("state"))
        out.append(
            {
                "key": drain_key("watch", wid),
                "kind": "watch",
                "identity": wid,
                "match": f"{wid} watch",
                "token": "",
                "generated_name": "",
                "generated": False,
                "label": label,
                "stale_label": obsidian_tabs.stale_label(label),
                "stale_reads": 3,
                **({"final": True} if d in final else {}),
                **stamp,
            }
        )
    return out


#: How long a worker that left the live set keeps its watch tab entry for a final paint.
FINAL_PAINT_GRACE_S = 5 * 60


def _left_at(root: Path, d: dict[str, Any]) -> datetime | None:
    """When the worker stopped being live, near enough: its newest `finished`/`actioned_at`
    stamp, else (a cancel writes neither) when its record last changed."""
    from ..dispatch import state

    stamps: list[datetime] = []
    for field in ("finished", "actioned_at"):
        with contextlib.suppress(TypeError, ValueError):
            stamp = datetime.fromisoformat(str(d.get(field)))
            if stamp.tzinfo is not None:
                stamps.append(stamp)
    with contextlib.suppress(OSError):
        mtime = state.file(root, str(d["id"]), ".json").stat().st_mtime
        stamps.append(datetime.fromtimestamp(mtime).astimezone())
    return max(stamps) if stamps else None


def build(root: Path) -> dict[str, Any]:
    """The snapshot, without `rendered_at`. Workers show live runs, unread verdicts and pending
    cleanup, with closed features filtered. `needs_you` and each manager's
    `manager_action` come from `obligations`."""
    from .. import dispatch as dispatch_mod
    from .. import features as features_mod
    from .. import leases as leases_mod
    from . import managers as managers_mod

    dispatches = dispatch_mod.load_all(root)
    features = features_mod.load_all(root)
    closed = {f.id for f in features if not f.is_open}
    by_id = {f.id: f for f in features}
    held = leases_mod.list_all(root)
    pairs = managers_mod.live_manager_records(root, held)
    owed = obligations(root, dispatches=dispatches, closed=closed, pairs=pairs)

    from ..dispatch import verdicts

    live_dispatches = [
        d
        for d in dispatches
        if dispatch_mod.is_live(d, closed)
        and (
            d.get("state") in ("queued", "running")
            or d.get("verification_cleanup_pending")
            or not verdicts.is_read(d)
        )
    ]
    workers = [_worker(root, d, by_id) for d in live_dispatches]
    managers = managers_mod.rows(
        root,
        dispatches=dispatches,
        closed_features=closed,
        held=held,
        pairs=pairs,
        actions=owed["manager_action"],
    )
    leases = [
        {
            "area": le.get("area"),
            "holder": le.get("session_id"),
            "feature": le.get("feature"),
            "taken_at": le.get("taken_at"),
            "touched_at": le.get("touched_at"),
            "note": le.get("note"),
            "status": leases_mod.status(le, root),
        }
        for le in held
    ]
    return {
        "observed_at": owed["observed_at"],
        "valid_until": owed["valid_until"],
        "managers": managers,
        "workers": workers,
        "leases": leases,
        "needs_you": owed["needs_you"],
        "manager_action": owed["manager_action"],
        "tabs": tab_entries(root, pairs, dispatches, live_dispatches, owed),
        "health": _health(root, owed),
    }


def _health(root: Path, owed: dict[str, Any]) -> dict[str, Any]:
    from ..telemetry.dashboard import live_health

    return live_health(root, stamp={"observed_at": owed["observed_at"], "valid_until": owed["valid_until"]})


def _stable(value: Any) -> Any:
    """`value` without the fields that change with the clock alone."""
    mapping, items = as_dict_or_none(value), as_list_or_none(value)
    if mapping is not None:
        return {k: _stable(v) for k, v in mapping.items() if k not in VOLATILE}
    if items is not None:
        return [_stable(v) for v in items]
    return value


def write(root: Path, *, heartbeat: bool = False) -> bool:
    """Write the snapshot when its content changed. A heartbeat also rewrites an unchanged one
    once it is `HEARTBEAT_SECONDS` old, so `rendered_at` tells a view the supervisor is still
    there, and any write renews one whose `valid_until` is less than half its window away.
    Returns True if written.

    One writer at a time, start to finish: capturing the facts (`build`),
    publishing or finding the snapshot unchanged, and clearing or recording the failure happen
    under `PUBLISH_LOCK`. Publication order is therefore capture order by construction, and no
    timestamp orders anything: a success always clears the failure record, a failure always
    replaces it. A `heartbeat` that cannot have the lock within `HEARTBEAT_WAIT_S` skips (False):
    the holder is publishing facts at least as fresh. Any other write waits `PUBLISH_WAIT_S`,
    then raises TimeoutError, which `write_quietly` records as a failed write: a hook's write
    never skips silently. `build` measured 0.23 s warm (median; 0.29 s max, 0.71 s cold) on a
    large copy of real state.

    Write-ahead: before anything else the write records its intent
    (`begin_intent`), which stands like a failure until this write ends. A writer killed during
    `build` (a hook past its client's timeout) runs no handler, and its intent keeps the
    snapshot from counting as authoritative. Every way out ends this thread's intents: a
    success clears them with the failures it found, a failure is recorded first, a skip had
    nothing to publish."""
    failed = False
    begin_intent(root)
    try:
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(_publish_lock(root, HEARTBEAT_WAIT_S if heartbeat else PUBLISH_WAIT_S))
            except TimeoutError as exc:
                if heartbeat:
                    return False
                # recorded here, before this write's intent ends, so the two never both lapse
                _keep_failure(root, exc)
                exc.add_note(RECORDED_NOTE)
                failed = True
                raise
            try:
                return _write_locked(root, heartbeat=heartbeat)
            except Exception as exc:
                _keep_failure(root, exc)
                exc.add_note(RECORDED_NOTE)
                failed = True
                raise
    finally:
        _end_intents(root)
        if failed:
            _failure_recorded(root)  # outside the lock


def _write_locked(root: Path, *, heartbeat: bool) -> bool:
    try:
        # the failures recorded before this write, in lock order, and the intents it may end:
        # its thread's own and those abandoned before the build reads the facts (`_abandoned`)
        seen = _clearable(root, _failures(root))
    except OSError:
        seen = []  # the folder cannot be listed: it keeps standing until it can
    snapshot = build(root)
    target = path(root)
    try:
        previous = as_dict_or_none(json.loads(target.read_text(encoding="utf-8")))
    except OSError, ValueError:
        previous = None
    if previous is not None:
        stamp = previous.pop("rendered_at", None)
        previous.pop("seq", None)  # written by older builds; no longer
        if _stable(previous) == _stable(json.loads(json.dumps(snapshot, default=str))):
            renew = _expiring(previous.get("observed_at"), previous.get("valid_until"))
            if not renew and (not heartbeat or not _older_than(stamp, HEARTBEAT_SECONDS)):
                _clear_failures(seen)  # the snapshot on disk holds these facts
                return False
    payload = {"rendered_at": core.iso(core.now()), **snapshot}
    core.atomic_write(target, json.dumps(payload, indent=2, default=str) + "\n")
    _clear_failures(seen)
    return True


PUBLISH_LOCK = "live.lock"
#: How long a write other than a heartbeat waits for `PUBLISH_LOCK`: past seven cold builds.
PUBLISH_WAIT_S = 5.0
#: How long the 5 s heartbeat waits: three warm builds; past it the holder is publishing anyway.
HEARTBEAT_WAIT_S = 1.0
#: Added to an exception whose failure `write` already recorded under the lock.
RECORDED_NOTE = "live snapshot failure recorded"


def _publish_lock(root: Path, wait: float | None = None) -> contextlib.AbstractContextManager[None]:
    """`tabs/live.lock` in the state folder, by the tab lock files' mechanism
    (`tab_reset.exclusive`): a lock a killed holder releases. Raises TimeoutError past `wait`
    (`PUBLISH_WAIT_S` by default)."""
    import time

    from ..tab_reset import exclusive

    return exclusive(root, PUBLISH_LOCK, time.monotonic() + (PUBLISH_WAIT_S if wait is None else wait))


#: Snapshot writes that failed, one file per failure, each written whole. A success removes exactly
#: the files it found when it took
#: `PUBLISH_LOCK`, so a failure recorded after that, even outside the lock by a writer that could
#: not have it, survives by construction. While the folder holds any, the snapshot on disk no
#: longer holds the facts: the plugin yields (its heartbeat says so), the fallback painter lets
#: saved requests newer than the snapshot win (`tab_drain._targets`), and `status check` calls
#: the snapshot stale. Intents (`INTENT_PREFIX`) live here too: writes in flight, which
#: suspend authority the same way; the plugin and `status` wait `INTENT_STALE_S` before taking
#: one for a writer that died.
WRITE_FAILED = core.agent_path(Path(), "live-failed")


def _keep_failure(root: Path, exc: BaseException) -> None:
    import uuid

    row = {"at": core.now().timestamp(), "error": f"{type(exc).__name__}: {exc}"[:300]}
    with contextlib.suppress(OSError):
        (root / WRITE_FAILED).mkdir(parents=True, exist_ok=True)
        core.atomic_write(root / WRITE_FAILED / f"{uuid.uuid4().hex}.json", json.dumps(row) + "\n")


def _failure_recorded(root: Path) -> None:
    """After a failure is recorded, outside the lock: the fallback takes over what is owed now,
    without waiting for another write (`tab_drain.on_snapshot_failure`)."""
    from ..tab_drain import on_snapshot_failure

    on_snapshot_failure(root)


def record_failure(root: Path, exc: BaseException) -> None:
    """Record a failed write that `write` did not record itself (it raised before taking the
    lock, or the write was not `write`): under `PUBLISH_LOCK`, so it lands in order; past the
    lock's wait it is kept anyway, since a lost failure is the worse error. Then hands what is
    owed to the fallback. Never raises."""
    if RECORDED_NOTE in getattr(exc, "__notes__", ()):
        return
    try:
        try:
            with _publish_lock(root):
                _keep_failure(root, exc)
        except TimeoutError:
            _keep_failure(root, exc)
        _failure_recorded(root)
    except Exception:  # noqa: BLE001, S110 — keeping the failure never raises in its place
        pass


def _failures(root: Path) -> list[Path]:
    """The standing failure files. Raises OSError when the folder exists but cannot be listed;
    only a missing folder means none stands."""
    try:
        return sorted(p for p in (root / WRITE_FAILED).iterdir() if p.suffix == ".json")
    except FileNotFoundError:
        return []


def failures_unreadable(root: Path) -> str | None:
    """Why the failure folder cannot be listed (a permissions error, a file in its place), else
    None. Such a folder counts as a failure standing, unreadable: the snapshot is not authoritative
    and `status check` is not fresh, with this reason."""
    try:
        _failures(root)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def write_failed(root: Path) -> float | None:
    """When the latest standing failure was recorded (wall clock, for display only; epoch
    seconds), or None when none stands. A record that cannot be read still stands (0.0), and so
    does a folder that cannot be listed (`failures_unreadable`). An intent (`begin_intent`)
    counts as standing too: this is the snapshot's authority, which a write in flight or one
    that died suspends; `failures_only` leaves intents out."""
    try:
        standing = _failures(root)
    except OSError:
        return 0.0
    return _latest(standing)


INTENT_PREFIX = "intent-"
"""An intent's file name in `WRITE_FAILED`: a write, or a fact a write is
to publish, is in flight. It stands like a failure, so the snapshot is not authoritative while it
does; `status` tells the two apart."""
INTENT_STALE_S = 10.0
"""An intent older than this with `PUBLISH_LOCK` free is a writer that died: `status check` is
not fresh, and the plugin yields to the fallback (its own `INTENT_STALE_MS`). A younger one is
in flight, and neither flickers."""
INTENT_REAP_S = 300.0
"""A write ends an intent another process left once that process is gone, or once it is this
old (a process that lives on, such as an MCP server, and never published)."""
INTENT_RUNNING_S = 30.0
"""An intent older than this is not fresh for `status` even while its write holds
`PUBLISH_LOCK`: a write stalled that long is not a normal build (0.23 s
warm, 0.71 s cold, a few seconds under load), so it never flickers on one."""
WATCH_MAX_S = 3600.0
"""How long a watchdog follows its intent: the longest a writer can live, an MCP call's 3,600 s
(a hook's is at most 600 s)."""
WATCH_BACKOFF_MAX_S = 60.0
"""The longest pause between two of a watchdog's looks; they start at 1 s and double."""

_thread = threading.local()


def _registry() -> list[Path]:
    paths: list[Path] = _thread.__dict__.setdefault("intents", [])
    return paths


def _mine(root: Path) -> list[Path]:
    """This thread's intents under `root`, not yet ended."""
    folder = root / WRITE_FAILED
    return [p for p in _registry() if p.parent == folder]


def begin_intent(root: Path, *, watch: bool = False) -> Path | None:
    """Record that a write is in flight, before the fact it will publish is persisted. The next `write`
    in this thread ends it, whatever its outcome, as
    `publish_pending` makes sure. With `watch` a detached watchdog (`watch_intent`) publishes
    the facts if the intent is still standing once its writer is gone: a question hook killed at
    its client's timeout leaves no later event to publish ✋. Never raises."""
    import uuid

    row = {"at": core.now().timestamp(), "intent": True, "pid": os.getpid()}
    target = root / WRITE_FAILED / f"{INTENT_PREFIX}{uuid.uuid4().hex}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        core.atomic_write(target, json.dumps(row) + "\n")
    except OSError as exc:
        sys.stderr.write(f"note: snapshot intent not recorded ({exc})\n")
        return None
    _registry().append(target)
    if watch:
        _watch(root, target)
    return target


def _end_intents(root: Path) -> None:
    mine = _mine(root)
    for intent in mine:
        with contextlib.suppress(OSError):
            intent.unlink(missing_ok=True)
    paths = _registry()
    paths[:] = [p for p in paths if p not in mine]


def publish_pending(root: Path) -> None:
    """Write the snapshot if this thread recorded an intent that no write has ended yet: a hook
    or op that persisted a fact and left on a path that wrote nothing. Never raises."""
    if _mine(root):
        write_quietly(root)


def _is_intent(path: Path) -> bool:
    return path.name.startswith(INTENT_PREFIX)


def _intent_row(path: Path) -> dict[str, Any] | None:
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None


def _intent_age(path: Path) -> float:
    """Seconds since the intent was recorded; past `INTENT_STALE_S` when unreadable."""
    at = (_intent_row(path) or {}).get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return INTENT_STALE_S + 1
    return core.now().timestamp() - float(at)


def _abandoned(path: Path) -> bool:
    """An intent whose process is gone, or past `INTENT_REAP_S`: no fact of its writer can still
    be persisted after this, so a build from now on holds them all."""
    row = _intent_row(path)
    if row is None or _intent_age(path) > INTENT_REAP_S:
        return True
    pid = row.get("pid")
    return not (isinstance(pid, int) and core.is_alive(pid))


_persisted: set[Path] = set()
_persisted_lock = threading.Lock()


def fact_persisted(intent: Path | None) -> None:
    """The fact `intent` was recorded for is on disk now: a write from any thread of this
    process may end it from here on, since its build reads the fact. A long-lived writer (the
    supervisor, an MCP server) may persist a fact in one thread and publish from another."""
    if intent is not None:
        with _persisted_lock:
            _persisted.add(intent)


def _clearable(root: Path, seen: list[Path]) -> list[Path]:
    """Of the files standing when a write took the lock: every failure, this thread's intents,
    intents whose fact this process has persisted (`fact_persisted`), and abandoned intents.
    Another live writer's intent stays: its fact may not be persisted yet, so this build may
    not hold it; its own write ends it."""
    mine = set(_mine(root))
    with _persisted_lock:
        mine |= _persisted
    return [p for p in seen if not _is_intent(p) or p in mine or _abandoned(p)]


def _forget(cleared: list[Path]) -> None:
    with _persisted_lock:
        _persisted.difference_update(cleared)


def failures_only(root: Path) -> float | None:
    """`write_failed` without the intents: when the latest standing failure was recorded."""
    try:
        standing = [p for p in _failures(root) if not _is_intent(p)]
    except OSError:
        return 0.0
    return _latest(standing)


def stale_intent(root: Path) -> str | None:
    """Why the snapshot's write is not done: an intent older than
    `INTENT_STALE_S` while nobody holds `PUBLISH_LOCK` is a writer that died; one older than
    `INTENT_RUNNING_S` is a write that has stalled, lock held or not. None while every intent is
    younger, or between the two while a write holds the lock (in flight)."""
    try:
        oldest = max((_intent_age(p) for p in _failures(root) if _is_intent(p)), default=0.0)
    except OSError:
        return None
    if oldest <= INTENT_STALE_S:
        return None
    try:
        with _publish_lock(root, 0.0):
            pass
    except TimeoutError:
        if oldest > INTENT_RUNNING_S:
            return f"a snapshot write has been running {int(oldest)} s"
        return None  # a write holds the lock: in flight
    except OSError:
        pass
    return f"a snapshot write did not finish ({int(oldest)} s ago; its writer is gone)"


def yield_due(root: Path) -> bool:
    """Whether the plugin should have yielded: a failure stands, the folder cannot be read, or
    an intent is older than `INTENT_STALE_S` (the plugin's own rule; a younger one is a write in
    flight, which it paints through)."""
    try:
        standing = _failures(root)
    except OSError:
        return True
    return any(not _is_intent(p) or _intent_age(p) > INTENT_STALE_S for p in standing)


def _watch(root: Path, intent: Path) -> None:
    """Start the detached watchdog for `intent` (`watch_intent`) when tabs are on."""
    try:
        from ..obsidian_tabs import enabled
        from ..tab_drain import spawn

        if enabled(root):
            spawn([sys.executable, "-m", "agent_cli.vault.live", "watch", str(root), intent.name], root)
    except Exception as exc:  # noqa: BLE001 — a watchdog is a backstop; the fact path goes on
        sys.stderr.write(f"note: snapshot watchdog not started ({exc})\n")


def _writer_gone(path: Path) -> bool:
    row = _intent_row(path)
    pid = (row or {}).get("pid")
    return row is None or not (isinstance(pid, int) and not isinstance(pid, bool) and core.is_alive(pid))


def watch_intent(root: Path, name: str, sleep: Any = None) -> bool:
    """The watchdog of an owner obligation's write (`begin_intent(watch=True)`). It follows the
    intent `name` until it is gone (its write ended it) or `WATCH_MAX_S` old, looking again
    after pauses that start at 1 s and double to `WATCH_BACKOFF_MAX_S`.
    Once the intent is stale (`INTENT_STALE_S`), on every look:

    - the plugin has yielded, so the fallback is started (`tab_drain.on_snapshot_failure`,
      which derives nothing itself): its runner derives the facts without `PUBLISH_LOCK`
      within `tab_drain.DERIVE_DEADLINE_S`, or paints the `?` forms while they are unknown;
    - its writer is gone: publish (`write_quietly`, which ends it and runs the expiry guard).
      A writer still alive (stalled in `build`, holding the lock) is looked at again.

    True if it published."""
    import time

    from ..tab_drain import on_snapshot_failure

    pause = sleep or time.sleep
    intent = root / WRITE_FAILED / name
    backoff = 1.0
    while intent.exists():
        age = _intent_age(intent)
        if age > WATCH_MAX_S:
            return False
        if age <= INTENT_STALE_S:
            pause(INTENT_STALE_S + 1 - age)
            continue
        # a runner first, which derives nothing here: the publication below may stall on
        # the same slow fact store the writer did, and the runner shows `?` meanwhile
        on_snapshot_failure(root)
        if _writer_gone(intent):
            write_quietly(root)
            if not intent.exists():
                return True
        pause(backoff)
        backoff = min(backoff * 2, WATCH_BACKOFF_MAX_S)
    return False


def _latest(standing: list[Path]) -> float | None:
    stamps: list[float] = []
    for failure in standing:
        try:
            at = as_dict(json.loads(failure.read_text(encoding="utf-8"))).get("at")
        except OSError, ValueError:
            at = None
        stamps.append(float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0)
    return max(stamps) if stamps else None


def _clear_failures(seen: list[Path]) -> None:
    """Remove the failures this success came after: the files that stood when it took
    `PUBLISH_LOCK`, which the caller still holds. Anything recorded since stands."""
    for failure in seen:
        with contextlib.suppress(OSError):
            failure.unlink(missing_ok=True)
    _forget(seen)


def write_for_tab(root: Path, session_id: str, label: str, *, timing: dict[str, Any] | None = None) -> None:
    """`write_quietly` when the snapshot's `tabs` entry for this manager does not read `label`:
    a hook that computed a new label without a state change (an ack, a raised question) brings
    the plugin's source up to date, and one that changed nothing costs a file read, not a
    rebuild."""
    from ..tab_drain import drain_key

    key = drain_key("manager", session_id)
    try:
        snap = as_dict(json.loads(path(root).read_text(encoding="utf-8")))
    except OSError, ValueError:
        snap = {}
    held = next((t for t in as_list(snap.get("tabs")) if as_dict(t).get("key") == key), None)
    # an expiring snapshot is renewed too, so a long turn of tool calls never reads as stale
    changed_timing = timing is not None and any(
        as_dict(held).get(k) != timing.get(k) for k in ("show_after", "before_label")
    )
    if (
        changed_timing
        or as_dict(held).get("label") != label
        or _expiring(snap.get("observed_at"), snap.get("valid_until"))
    ):
        write_quietly(root)


def write_quietly(root: Path, *, heartbeat: bool = False) -> None:
    """`write` for hooks and the supervisor: a snapshot failure must never fail the caller.

    Then the tab expiry guard (`tab_drain.ensure_expiry_guard`): the writer of a snapshot that
    expires is what keeps its expiry painted when the plugin is not healthy. It starts a drainer
    only then, and never raises."""
    try:
        write(root, heartbeat=heartbeat)
    except Exception as exc:  # noqa: BLE001 — the view shows a stale badge; the next write catches up
        record_failure(root, exc)
        sys.stderr.write(f"note: live snapshot failed ({exc})\n")
    from ..tab_drain import ensure_expiry_guard

    ensure_expiry_guard(root)


def _expiring(observed: Any, until: Any) -> bool:
    """Less than half the derivation's window left, or no readable window at all."""
    try:
        start, end = datetime.fromisoformat(str(observed)), datetime.fromisoformat(str(until))
    except TypeError, ValueError:
        return True
    return (end - core.now()).total_seconds() < (end - start).total_seconds() / 2


def _older_than(stamp: Any, seconds: float) -> bool:
    try:
        return (core.now() - datetime.fromisoformat(str(stamp))).total_seconds() >= seconds
    except TypeError, ValueError:
        return True


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "watch":
    from agent_cli.vault import live as _live  # the imported module, whose state others share

    _live.watch_intent(Path(sys.argv[2]), sys.argv[3])
