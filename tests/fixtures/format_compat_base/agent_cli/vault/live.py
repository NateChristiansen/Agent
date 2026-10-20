# ruff: noqa
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
TAIL_BYTES = 64 * 1024


def path(root: Path) -> Path:
    return root / LIVE_JSON


def last_tool(out_path: Path) -> str | None:
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
            continue
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
    feature = features_by_id.get(d.get("feature"))
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


VALID_RUNNING_S = 3 * HEARTBEAT_SECONDS
VALID_IDLE_S = 12 * 60 * 60
VOLATILE = frozenset({"observed_at", "valid_until"})


def age_s(since: Any, moment: datetime | None = None) -> int | None:
    try:
        return max(0, int(((moment or core.now()) - datetime.fromisoformat(str(since))).total_seconds()))
    except TypeError, ValueError:
        return None


def _earliest(stamps: list[Any]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for stamp in stamps:
        try:
            parsed.append((datetime.fromisoformat(str(stamp)), str(stamp)))
        except TypeError, ValueError:
            continue
    try:
        return min(parsed)[1] if parsed else None
    except TypeError:
        return None


def _resume_action(root: Path, items: list[dict[str, Any]]) -> dict[str, Any]:
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
    from ..dispatch import verdicts
    from ..dispatch.state import CLOSED_STATES, is_actioned, is_probe

    state = d.get("state")
    if is_probe(d) or state in ("queued", "running"):
        return None
    cleanup = bool(d.get("verification_cleanup_pending"))
    if (state in CLOSED_STATES or is_actioned(d)) and (not cleanup):
        return None
    if state in ("question", "blocked") and (not is_actioned(d)):
        return ("owner", (str(state),))
    reasons = tuple(
        (
            reason
            for reason, owed in (
                ("cleanup", cleanup),
                ("unread", not verdicts.is_read(d)),
                ("unresolved-branch", verdicts.unresolved_branch(d)),
            )
            if owed
        )
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
    running_any = any((running_dispatch(d) for d in dispatches))
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
            "since": d.get("finished"),
            "read": verdicts.is_read(d),
            "launched_by_session": d.get("launched_by_session"),
            **stamp,
        }
        if owed[0] == "owner":
            workers.append(item)
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
"The `attention_for` projections that are the owner's to act on: the two a manager is listed\nunder in `needs_you` (`build`), and the ✋ on its tab."


def running_dispatch(d: dict[str, Any]) -> bool:
    return d.get("state") in ("queued", "running") and core.is_alive(d.get("supervisor_pid"))


def approval_show_after(record: dict[str, Any]) -> str | None:
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
    from ..hooks.task_waits import PERMISSION_FIELD
    from ..peers import pending_waits

    if (
        running
        or dig(record, "activity", "state") != "idle"
        or any(
            (
                record.get(k)
                for k in ("background_tasks", "session_crons", "owner_question_pending", PERMISSION_FIELD)
            )
        )
    ):
        return None
    waits = pending_waits(record)
    return max((str(w["until"]) for w in waits.values()), default=None)


def owner_obligation(record: dict[str, Any]) -> str | None:
    reason = attention_for(record, 0, [])
    return reason if reason in OWNER_ATTENTION else None


def tab_entries(
    root: Path,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    dispatches: list[dict[str, Any]],
    live_workers: list[dict[str, Any]],
    owed: dict[str, Any],
) -> list[dict[str, Any]]:
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
        except Exception as exc:
            sys.stderr.write(f"note: tab label for {sid[:8]} failed ({exc})\n")
            continue
        if not sid or desired is None:
            continue
        name, _, label = desired
        peer_until = peer_wait_until(
            record, sum((running_dispatch(d) for d in dispatches if d.get("launched_by_session") == sid))
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


FINAL_PAINT_GRACE_S = 5 * 60


def _left_at(root: Path, d: dict[str, Any]) -> datetime | None:
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
            or (not verdicts.is_read(d))
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
        "health": _health(root),
    }


def _health(root: Path) -> dict[str, Any]:
    from ..telemetry.dashboard import live_health

    return live_health(root)


def _stable(value: Any) -> Any:
    mapping, items = (as_dict_or_none(value), as_list_or_none(value))
    if mapping is not None:
        return {k: _stable(v) for k, v in mapping.items() if k not in VOLATILE}
    if items is not None:
        return [_stable(v) for v in items]
    return value


def write(root: Path, *, heartbeat: bool = False) -> bool:
    failed = False
    begin_intent(root)
    try:
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(_publish_lock(root, HEARTBEAT_WAIT_S if heartbeat else PUBLISH_WAIT_S))
            except TimeoutError as exc:
                if heartbeat:
                    return False
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
            _failure_recorded(root)


def _write_locked(root: Path, *, heartbeat: bool) -> bool:
    try:
        seen = _clearable(root, _failures(root))
    except OSError:
        seen = []
    snapshot = build(root)
    target = path(root)
    try:
        previous = as_dict_or_none(json.loads(target.read_text(encoding="utf-8")))
    except OSError, ValueError:
        previous = None
    if previous is not None:
        stamp = previous.pop("rendered_at", None)
        previous.pop("seq", None)
        if _stable(previous) == _stable(json.loads(json.dumps(snapshot, default=str))):
            renew = _expiring(previous.get("observed_at"), previous.get("valid_until"))
            if not renew and (not heartbeat or not _older_than(stamp, HEARTBEAT_SECONDS)):
                _clear_failures(seen)
                return False
    payload = {"rendered_at": core.iso(core.now()), **snapshot}
    core.atomic_write(target, json.dumps(payload, indent=2, default=str) + "\n")
    _clear_failures(seen)
    return True


PUBLISH_LOCK = "live.lock"
PUBLISH_WAIT_S = 5.0
HEARTBEAT_WAIT_S = 1.0
RECORDED_NOTE = "live snapshot failure recorded"


def _publish_lock(root: Path, wait: float | None = None) -> contextlib.AbstractContextManager[None]:
    import time
    from ..tab_reset import exclusive

    return exclusive(root, PUBLISH_LOCK, time.monotonic() + (PUBLISH_WAIT_S if wait is None else wait))


WRITE_FAILED = core.agent_path(Path(), "live-failed")


def _keep_failure(root: Path, exc: BaseException) -> None:
    import uuid

    row = {"at": core.now().timestamp(), "error": f"{type(exc).__name__}: {exc}"[:300]}
    with contextlib.suppress(OSError):
        (root / WRITE_FAILED).mkdir(parents=True, exist_ok=True)
        core.atomic_write(root / WRITE_FAILED / f"{uuid.uuid4().hex}.json", json.dumps(row) + "\n")


def _failure_recorded(root: Path) -> None:
    from ..tab_drain import on_snapshot_failure

    on_snapshot_failure(root)


def record_failure(root: Path, exc: BaseException) -> None:
    if RECORDED_NOTE in getattr(exc, "__notes__", ()):
        return
    try:
        try:
            with _publish_lock(root):
                _keep_failure(root, exc)
        except TimeoutError:
            _keep_failure(root, exc)
        _failure_recorded(root)
    except Exception:
        pass


def _failures(root: Path) -> list[Path]:
    try:
        return sorted((p for p in (root / WRITE_FAILED).iterdir() if p.suffix == ".json"))
    except FileNotFoundError:
        return []


def failures_unreadable(root: Path) -> str | None:
    try:
        _failures(root)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def write_failed(root: Path) -> float | None:
    try:
        standing = _failures(root)
    except OSError:
        return 0.0
    return _latest(standing)


INTENT_PREFIX = "intent-"
"An intent's file name in `WRITE_FAILED`: a write, or a fact a write is\nto publish, is in flight. It stands like a failure, so the snapshot is not authoritative while it\ndoes; `status` tells the two apart."
INTENT_STALE_S = 10.0
"An intent older than this with `PUBLISH_LOCK` free is a writer that died: `status check` is\nnot fresh, and the plugin yields to the fallback (its own `INTENT_STALE_MS`). A younger one is\nin flight, and neither flickers."
INTENT_REAP_S = 300.0
"A write ends an intent another process left once that process is gone, or once it is this\nold (a process that lives on, such as an MCP server, and never published)."
INTENT_RUNNING_S = 30.0
"An intent older than this is not fresh for `status` even while its write holds\n`PUBLISH_LOCK`: a write stalled that long is not a normal build (0.23 s\nwarm, 0.71 s cold, a few seconds under load), so it never flickers on one."
WATCH_MAX_S = 3600.0
"How long a watchdog follows its intent: the longest a writer can live, an MCP call's 3,600 s\n(a hook's is at most 600 s)."
WATCH_BACKOFF_MAX_S = 60.0
"The longest pause between two of a watchdog's looks; they start at 1 s and double."
_thread = threading.local()


def _registry() -> list[Path]:
    paths: list[Path] = _thread.__dict__.setdefault("intents", [])
    return paths


def _mine(root: Path) -> list[Path]:
    folder = root / WRITE_FAILED
    return [p for p in _registry() if p.parent == folder]


def begin_intent(root: Path, *, watch: bool = False) -> Path | None:
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
    at = (_intent_row(path) or {}).get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return INTENT_STALE_S + 1
    return core.now().timestamp() - float(at)


def _abandoned(path: Path) -> bool:
    row = _intent_row(path)
    if row is None or _intent_age(path) > INTENT_REAP_S:
        return True
    pid = row.get("pid")
    return not (isinstance(pid, int) and core.is_alive(pid))


_persisted: set[Path] = set()
_persisted_lock = threading.Lock()


def fact_persisted(intent: Path | None) -> None:
    if intent is not None:
        with _persisted_lock:
            _persisted.add(intent)


def _clearable(root: Path, seen: list[Path]) -> list[Path]:
    mine = set(_mine(root))
    with _persisted_lock:
        mine |= _persisted
    return [p for p in seen if not _is_intent(p) or p in mine or _abandoned(p)]


def _forget(cleared: list[Path]) -> None:
    with _persisted_lock:
        _persisted.difference_update(cleared)


def failures_only(root: Path) -> float | None:
    try:
        standing = [p for p in _failures(root) if not _is_intent(p)]
    except OSError:
        return 0.0
    return _latest(standing)


def stale_intent(root: Path) -> str | None:
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
        return None
    except OSError:
        pass
    return f"a snapshot write did not finish ({int(oldest)} s ago; its writer is gone)"


def yield_due(root: Path) -> bool:
    try:
        standing = _failures(root)
    except OSError:
        return True
    return any((not _is_intent(p) or _intent_age(p) > INTENT_STALE_S for p in standing))


def _watch(root: Path, intent: Path) -> None:
    try:
        from ..obsidian_tabs import enabled
        from ..tab_drain import spawn

        if enabled(root):
            spawn([sys.executable, "-m", "agent_cli.vault.live", "watch", str(root), intent.name], root)
    except Exception as exc:
        sys.stderr.write(f"note: snapshot watchdog not started ({exc})\n")


def _writer_gone(path: Path) -> bool:
    row = _intent_row(path)
    pid = (row or {}).get("pid")
    return row is None or not (isinstance(pid, int) and (not isinstance(pid, bool)) and core.is_alive(pid))


def watch_intent(root: Path, name: str, sleep: Any = None) -> bool:
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
        stamps.append(float(at) if isinstance(at, (int, float)) and (not isinstance(at, bool)) else 0.0)
    return max(stamps) if stamps else None


def _clear_failures(seen: list[Path]) -> None:
    for failure in seen:
        with contextlib.suppress(OSError):
            failure.unlink(missing_ok=True)
    _forget(seen)


def write_for_tab(root: Path, session_id: str, label: str, *, timing: dict[str, Any] | None = None) -> None:
    from ..tab_drain import drain_key

    key = drain_key("manager", session_id)
    try:
        snap = as_dict(json.loads(path(root).read_text(encoding="utf-8")))
    except OSError, ValueError:
        snap = {}
    held = next((t for t in as_list(snap.get("tabs")) if as_dict(t).get("key") == key), None)
    changed_timing = timing is not None and any(
        (as_dict(held).get(k) != timing.get(k) for k in ("show_after", "before_label"))
    )
    if (
        changed_timing
        or as_dict(held).get("label") != label
        or _expiring(snap.get("observed_at"), snap.get("valid_until"))
    ):
        write_quietly(root)


def write_quietly(root: Path, *, heartbeat: bool = False) -> None:
    try:
        write(root, heartbeat=heartbeat)
    except Exception as exc:
        record_failure(root, exc)
        sys.stderr.write(f"note: live snapshot failed ({exc})\n")
    from ..tab_drain import ensure_expiry_guard

    ensure_expiry_guard(root)


def _expiring(observed: Any, until: Any) -> bool:
    try:
        start, end = (datetime.fromisoformat(str(observed)), datetime.fromisoformat(str(until)))
    except TypeError, ValueError:
        return True
    return (end - core.now()).total_seconds() < (end - start).total_seconds() / 2


def _older_than(stamp: Any, seconds: float) -> bool:
    try:
        return (core.now() - datetime.fromisoformat(str(stamp))).total_seconds() >= seconds
    except TypeError, ValueError:
        return True


if __name__ == "__main__" and len(sys.argv) == 4 and (sys.argv[1] == "watch"):
    from agent_cli.vault import live as _live

    _live.watch_intent(Path(sys.argv[2]), sys.argv[3])
