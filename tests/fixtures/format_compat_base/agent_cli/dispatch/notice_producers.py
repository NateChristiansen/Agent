# ruff: noqa
from __future__ import annotations
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .. import peers
from ..core import AgentError
from ..records.narrow import as_dict_or_none, dict_list, dig
from . import notice_delivery, notices
from .state import load, load_all, save, verification_summary

RECONCILE_LIMIT = 3
ROUTE_FIELD = "notice_route"


def _note(what: str, exc: Exception) -> None:
    sys.stderr.write(f"note: dispatch notice {what} skipped ({type(exc).__name__})\n")


def _clock() -> float:
    return notice_delivery.wall_clock()


def _ring(target: dict[str, Any], name: str) -> tuple[bool, str, str | None]:
    try:
        return notice_delivery.ring_dispatch(target, name)
    except Exception as exc:
        _note("ring", exc)
        return (False, "refused", "transport-refused")


def caller_session(root: Path, explicit: str | None = None) -> str | None:
    from .. import sessions

    if explicit:
        return explicit
    try:
        for pid in sessions.candidate_pids():
            sid = sessions.read_by_pid(root, pid)
            if sid:
                return sid
    except AgentError, OSError, ValueError:
        pass
    for var in sessions.ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


def new_route(root: Path, launched_by_session: str | None) -> dict[str, Any] | None:
    if not notices.session_ok(launched_by_session):
        return None
    try:
        return notices.route(notices.harness_identity(root), str(launched_by_session))
    except (AgentError, OSError) as exc:
        _note("route", exc)
        return None


def launch_route(root: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    from .. import sessions

    if ROUTE_FIELD in record:
        return record[ROUTE_FIELD]
    sid = record.get("launched_by_session")
    if not notices.session_ok(sid):
        return None
    try:
        found = as_dict_or_none(sessions.get(root, str(sid)))
    except AgentError, OSError, ValueError:
        return None
    if (
        found is None
        or found.get("session_id", sid) != sid
        or found.get("dispatch_id")
        or found.get("remote")
        or (dig(found, "activity", "state") == "ended")
    ):
        return None
    return new_route(root, str(sid))


def recipient(entry: dict[str, Any]) -> str | None:
    envelope = as_dict_or_none(entry.get("envelope"))
    if envelope is not None:
        value = envelope.get("recipient")
    else:
        value = dig(entry, "route", "launched_by_session")
    return value if isinstance(value, str) else None


def record(
    root: Path, current: dict[str, Any], disposition: notices.Disposition, *, consumption: str = "none"
) -> dict[str, Any] | None:
    try:
        return notices.record_intent(
            current,
            disposition,
            harness=notices.harness_identity(root),
            launch_route=launch_route(root, current),
            consumption=consumption,
        )
    except (AgentError, OSError) as exc:
        _note("intent", exc)
        return None


def _verification(current: dict[str, Any]) -> str:
    return notices.assess_verification(verification_summary(current)["status"])


def terminal(root: Path, current: dict[str, Any], sent: list[str]) -> None:
    state = current["state"]
    if state not in notices.TERMINAL_NOTICE_STATES:
        if state in ("cancelled", "abandoned"):
            supersede(current, "cancel" if state == "cancelled" else "abandon")
        return
    disposition = notices.Disposition(
        current["attempt"], "terminal", state, _verification(current), None, False, None
    )
    _keep(record(root, current, disposition), sent)


def cleanup_incomplete(root: Path, current: dict[str, Any], sent: list[str]) -> None:
    disposition = notices.Disposition(
        current["attempt"], "attention", current["state"], "incomplete", None, True, "cleanup-incomplete"
    )
    _keep(record(root, current, disposition), sent)


def _keep(entry: dict[str, Any] | None, sent: list[str]) -> None:
    if entry is not None and entry.get("consumption") == "none" and (entry.get("delivery") != "superseded"):
        sent.append(entry["notice_id"])


def supersede(current: dict[str, Any], by: str) -> list[str]:
    return notices.supersede(current, by=by, at=notices.utc_stamp())


def acknowledge(current: dict[str, Any]) -> None:
    for entry in notices.outbox(current).values():
        if entry.get("consumption") == "none":
            entry["consumption"] = "acknowledged"


def deliver(root: Path, dispatch_id: str, nid: str) -> notice_delivery.Outcome | None:
    try:
        return notice_delivery.deliver(root, dispatch_id, nid, transport=_ring, clock=_clock)
    except Exception as exc:
        _note("delivery", exc)
        return None


@contextmanager
def delivering(root: Path, dispatch_id: str) -> Generator[list[str]]:
    sent: list[str] = []
    try:
        yield sent
    finally:
        for nid in sent:
            deliver(root, dispatch_id, nid)


def question_hold(root: Path, dispatch_id: str) -> None:
    from .verification_gate import record_guard

    try:
        with delivering(root, dispatch_id) as sent, record_guard(root, dispatch_id) as current:
            if current.get("state") != "running" or current.get("terminal_committed"):
                return
            disposition = notices.Disposition(
                current["attempt"],
                "attention",
                "running",
                _verification(current),
                None,
                False,
                "question-pending",
            )
            entry = record(root, current, disposition)
            asked = len(dict_list(current.get("questions_held"))) + 1
            question_id = f"{current['attempt']}-q{asked}"
            current["questions_held"] = [
                *dict_list(current.get("questions_held")),
                {"id": question_id, "attempt": current["attempt"], "held": notices.utc_stamp()},
            ]
            current["open_question"] = question_id
            save(root, current)
            _keep(entry, sent)
        _transition(root, current, "awaiting-answer", question_id)
    except (AgentError, OSError) as exc:
        _note("intent", exc)


def record_answer(d: dict[str, Any], by: str, text: str) -> None:
    from ..core import iso, now

    d["thread"].append({"ts": iso(now()), "from": by, "text": text})
    question_id = d.pop("open_question", None)
    if question_id:
        d["answered_question"] = {"id": question_id, "attempt": int(d["attempt"]) + 1}


def _transition(root: Path, current: dict[str, Any], phase: str, question_id: str) -> None:
    from . import telemetry_events

    try:
        telemetry_events.transition(root, current, phase, question_id=question_id)
    except Exception as exc:
        _note("transition", exc)


def answered(root: Path, dispatch_id: str) -> None:
    from .verification_gate import record_guard

    try:
        with record_guard(root, dispatch_id) as current:
            question_id = str(current.pop("open_question", "") or "")
            if supersede(current, "answer"):
                current["chat_pending"] = False
            save(root, current)
        if question_id:
            _transition(root, current, "model-active", question_id)
    except (AgentError, OSError) as exc:
        _note("supersession", exc)


def reverify_complete(
    root: Path, current: dict[str, Any], run_id: str, status: str, cleaned: bool, caller: str | None
) -> str | None:
    verification = notices.assess_verification(status) if cleaned else "incomplete"
    disposition = notices.Disposition(
        current["attempt"], "verification-complete", current["state"], verification, run_id, not cleaned, None
    )
    return _for_caller(root, current, disposition, caller)


def _for_caller(
    root: Path, current: dict[str, Any], disposition: notices.Disposition, caller: str | None
) -> str | None:
    route = launch_route(root, current)
    own = caller is not None and isinstance(route, dict) and (route.get("launched_by_session") == caller)
    entry = record(root, current, disposition, consumption="show-consumed" if own else "none")
    if own and entry is not None:
        from . import verdicts

        if verdicts.has_verdict(current):
            verdicts.mark(current, str(caller), "reverify")
    if entry is None or own:
        return None
    sent: list[str] = []
    _keep(entry, sent)
    return sent[0] if sent else None


def reverify_raised(root: Path, dispatch_id: str, run_id: str | None, caller: str | None) -> None:
    from .verification_gate import record_guard

    if run_id is None:
        return
    nid = None
    try:
        with record_guard(root, dispatch_id) as current:
            row = next(
                (r for r in dict_list(current.get("reverify_runs")) if dig(r, "binding", "run_id") == run_id),
                None,
            )
            if row is None or dig(row, "result", "status") != "incomplete":
                return
            disposition = notices.Disposition(
                current["attempt"],
                "attention",
                current["state"],
                "incomplete",
                run_id,
                bool(row.get("cleanup_pending")),
                "verification-incomplete",
            )
            nid = _for_caller(root, current, disposition, caller)
            if nid is None:
                current["chat_pending"] = True
            save(root, current)
    except (AgentError, OSError) as exc:
        _note("intent", exc)
        return
    if nid is not None:
        deliver(root, dispatch_id, nid)


def consume(
    root: Path, dispatch_id: str, observed: dict[str, int], caller: str
) -> tuple[list[str], str | None]:
    consumed: list[str] = []
    try:
        guard = notice_delivery.bounded_record_guard(root, dispatch_id, notice_delivery.GUARD_TIMEOUT_S)
        with notice_delivery.step(guard) as current:
            for nid, generation in observed.items():
                entry = notices.actionable(current, nid)
                if (
                    entry is not None
                    and entry.get("generation") == generation
                    and (recipient(entry) == caller)
                ):
                    entry["consumption"] = "show-consumed"
                    consumed.append(nid)
            if not any((notices.actionable(current, nid) for nid in notices.outbox(current))):
                current["chat_pending"] = False
            save(root, current)
    except notice_delivery.Unavailable as exc:
        return ([], exc.failure)
    if consumed:
        try:
            with notice_delivery.step(
                peers.inbox_guard(root, caller, timeout=notice_delivery.GUARD_TIMEOUT_S)
            ):
                notice_delivery.tombstone_all(root, caller, consumed, "show-consumed", clock=_clock)
        except notice_delivery.Unavailable:
            pass
    return (consumed, None)


def _clear_observed(root: Path, d: dict[str, Any]) -> bool:
    try:
        guard = notice_delivery.bounded_record_guard(root, d["id"], notice_delivery.GUARD_TIMEOUT_S)
        with notice_delivery.step(guard) as current:
            if notices.generation(current) != notices.generation(d):
                return False
            current["chat_pending"] = False
            save(root, current)
    except notice_delivery.Unavailable:
        return False
    return True


def show(root: Path, d: dict[str, Any], caller: str | None = None) -> dict[str, Any]:
    caller = caller or caller_session(root)
    entries = notices.outbox(d)
    current = next((e for e in entries.values() if e.get("generation") == notices.generation(d)), None)
    owner = recipient(current) if current is not None else None
    if owner is None:
        from .verdicts import may_clear_pending

        if d.get("chat_pending") and may_clear_pending(root, d, caller) and _clear_observed(root, d):
            return load(root, d["id"])
        return d
    observed = {
        nid: entry["generation"]
        for nid in entries
        if (entry := notices.actionable(d, nid)) is not None and recipient(entry) == caller
    }
    report: dict[str, Any] = {"caller": caller, "consumed": [], "failure": None}
    if caller is not None and caller == owner and (observed or d.get("chat_pending")):
        consumed, failure = consume(root, d["id"], observed, caller)
        report.update(consumed=consumed, failure=failure)
        if failure is None:
            d = load(root, d["id"])
    d["notice_show"] = report
    return d


def consume_read(root: Path, dispatch_id: str, caller: str | None) -> None:
    d = load(root, dispatch_id)
    if d.get("notice_outbox"):
        d = show(root, d, caller)
    if d.get("chat_pending"):
        _clear_observed(root, d)


@dataclass(frozen=True)
class Candidate:
    recorded_at: str
    dispatch_id: str
    notice_id: str
    generation: int
    recipient: str | None
    envelope: dict[str, Any]


def candidates(root: Path, dispatch_ids: list[str] | None = None) -> list[Candidate]:
    records: list[dict[str, Any]]
    if dispatch_ids is None:
        try:
            records = load_all(root)
        except AgentError, OSError, ValueError:
            return []
    else:
        records = []
        for did in dispatch_ids:
            try:
                records.append(load(root, did))
            except AgentError, OSError, ValueError:
                continue
    out: list[Candidate] = []
    for rec in records:
        try:
            ids = notice_delivery.eligible(rec, clock=_clock)
        except AgentError, ValueError, TypeError:
            continue
        for nid in ids:
            entry = notices.outbox(rec)[nid]
            out.append(
                Candidate(
                    str(entry.get("recorded_at") or ""),
                    str(rec["id"]),
                    nid,
                    int(entry["generation"]),
                    recipient(entry),
                    entry["envelope"],
                )
            )
    return sorted(out, key=lambda c: (c.recorded_at, c.dispatch_id))


def reconcile(
    root: Path, dispatch_ids: list[str] | None = None, *, limit: int = RECONCILE_LIMIT
) -> list[notice_delivery.Outcome]:
    from .terminal import recover_pending

    recover_pending(root, dispatch_ids, limit=limit)
    out: list[notice_delivery.Outcome] = []
    for item in candidates(root, dispatch_ids)[:limit]:
        outcome = deliver(root, item.dispatch_id, item.notice_id)
        if outcome is not None:
            out.append(outcome)
    return out
