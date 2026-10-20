"""Where a dispatch's lifecycle produces its notices, and how stranded ones are recovered.
A producer records its intent with `notices.record_intent` inside the guarded save that
exposes the disposition, keeps the entry it got back, and asks `notice_delivery.deliver` for
one attempt only after that guard is released. The lifecycle never waits on a notice and never
fails because of one: an intent that cannot be built is dropped with a note on stderr, and a
delivery failure is audit on the entry, never the dispatch's state, outcome, evidence, leases
or cost.

Consumption here is delivery bookkeeping: whether a notice may be offered again. Whether a
manager read the verdict is a separate fact, `verdicts.FIELD`, which `dispatch consume` and the
legacy `show` alias write; a `hook-consumed` notice never counts as read.

Show consumes what it returns to its exact recipient in two steps,
the source entry under the record guard and then the recipient's tombstone under the inbox
guard, never both at once. A crash between them is safe: a consumed source entry is no longer
actionable, so nothing replays it.
"""

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
    except Exception as exc:  # noqa: BLE001 — a raising transport is a refused ring
        _note("ring", exc)
        return False, "refused", "transport-refused"


# ------------------------------------------------------------------ identities and routes


def caller_session(root: Path, explicit: str | None = None) -> str | None:
    """The calling manager's session by the dispatch tool's own order: explicit, then the
    by-pid record of this process or its parents, then the client's session variable."""
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
    """The version-1 local route `dispatch new` persists, or None without a launching session."""
    if not notices.session_ok(launched_by_session):
        return None
    try:
        return notices.route(notices.harness_identity(root), str(launched_by_session))
    except (AgentError, OSError) as exc:
        _note("route", exc)
        return None


def launch_route(root: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """The record's persisted route. A legacy record without one gets the same route from its
    `launched_by_session` only while an exact local manager session record names that id."""
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
        or dig(found, "activity", "state") == "ended"
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
    """`notices.record_intent` for this harness and the record's route. Call under the record
    guard, before the save that exposes the disposition."""
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
    """A committed stop: a terminal notice for done, blocked, failed or question; a cancel
    supersedes instead."""
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
    if entry is not None and entry.get("consumption") == "none" and entry.get("delivery") != "superseded":
        sent.append(entry["notice_id"])


def supersede(current: dict[str, Any], by: str) -> list[str]:
    """Retire every outstanding entry, keeping its audit, in the caller's guarded save."""
    return notices.supersede(current, by=by, at=notices.utc_stamp())


def acknowledge(current: dict[str, Any]) -> None:
    for entry in notices.outbox(current).values():
        if entry.get("consumption") == "none":
            entry["consumption"] = "acknowledged"


def deliver(root: Path, dispatch_id: str, nid: str) -> notice_delivery.Outcome | None:
    try:
        return notice_delivery.deliver(root, dispatch_id, nid, transport=_ring, clock=_clock)
    except Exception as exc:  # noqa: BLE001 — a notice never fails the lifecycle that produced it
        _note("delivery", exc)
        return None


@contextmanager
def delivering(root: Path, dispatch_id: str) -> Generator[list[str]]:
    """Collect the ids a guarded block produced; deliver them once the block, and the record
    guard entered after this, have exited."""
    sent: list[str] = []
    try:
        yield sent
    finally:
        for nid in sent:
            deliver(root, dispatch_id, nid)


def question_hold(root: Path, dispatch_id: str) -> None:
    """A worker's question opened its live answer hold: one guarded save for the attention
    intent, then delivery while the worker keeps its lease."""
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
            # The hold is a lifecycle phase, not just a notice: `dispatch spans` read
            # the wait as model time because nothing marked it. One id ties hold and answer.
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
    """A resume's answer on the thread. An answer to an open question is carried to the attempt
    it starts, whose `model-active` transition names the question (`AttemptClock`), so a hold
    answered by resume is correlated as a live answer is."""
    from ..core import iso, now

    d["thread"].append({"ts": iso(now()), "from": by, "text": text})
    question_id = d.pop("open_question", None)
    if question_id:
        d["answered_question"] = {"id": question_id, "attempt": int(d["attempt"]) + 1}


def _transition(root: Path, current: dict[str, Any], phase: str, question_id: str) -> None:
    from . import telemetry_events

    try:
        telemetry_events.transition(root, current, phase, question_id=question_id)
    except Exception as exc:  # noqa: BLE001 - telemetry never stops a worker's answer
        _note("transition", exc)


def answered(root: Path, dispatch_id: str) -> None:
    """An answer continued the live attempt, so its question-pending notice is retired."""
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
    """The run's verification-complete intent, under its final guard. Returns the id to deliver,
    or None when the caller is the exact launcher, who reads the result from the tool itself."""
    verification = notices.assess_verification(status) if cleaned else "incomplete"
    disposition = notices.Disposition(
        current["attempt"], "verification-complete", current["state"], verification, run_id, not cleaned, None
    )
    return _for_caller(root, current, disposition, caller)


def _for_caller(
    root: Path, current: dict[str, Any], disposition: notices.Disposition, caller: str | None
) -> str | None:
    route = launch_route(root, current)
    own = caller is not None and isinstance(route, dict) and route.get("launched_by_session") == caller
    entry = record(root, current, disposition, consumption="show-consumed" if own else "none")
    if own and entry is not None:
        from . import verdicts

        if verdicts.has_verdict(current):
            verdicts.mark(current, str(caller), "reverify")  # the launcher read it from the tool
    if entry is None or own:
        return None
    sent: list[str] = []
    _keep(entry, sent)
    return sent[0] if sent else None


def reverify_raised(root: Path, dispatch_id: str, run_id: str | None, caller: str | None) -> None:
    """A run that raised after persisting its incomplete row gets one attention notice. Its own
    launcher is not rung but keeps the pending-record fallback."""
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
    """Mark the observed generations show-consumed for their exact recipient: the source under
    the record guard, then the recipient's tombstones under the inbox guard. Returns the ids
    consumed and a fixed failure class when the source save failed, which consumes nothing."""
    consumed: list[str] = []
    try:
        guard = notice_delivery.bounded_record_guard(root, dispatch_id, notice_delivery.GUARD_TIMEOUT_S)
        with notice_delivery.step(guard) as current:
            for nid, generation in observed.items():
                entry = notices.actionable(current, nid)
                if entry is not None and entry.get("generation") == generation and recipient(entry) == caller:
                    entry["consumption"] = "show-consumed"
                    consumed.append(nid)
            if not any(notices.actionable(current, nid) for nid in notices.outbox(current)):
                current["chat_pending"] = False
            save(root, current)
    except notice_delivery.Unavailable as exc:
        return [], exc.failure
    if consumed:
        try:
            with notice_delivery.step(
                peers.inbox_guard(root, caller, timeout=notice_delivery.GUARD_TIMEOUT_S)
            ):
                notice_delivery.tombstone_all(root, caller, consumed, "show-consumed", clock=_clock)
        except notice_delivery.Unavailable:
            pass  # the consumed source entry already keeps it from being offered again
    return consumed, None


def _clear_observed(root: Path, d: dict[str, Any]) -> bool:
    """Clear `chat_pending` only while the record still holds the generation `d` observed."""
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
    """`dispatch show` for a record with typed notices. Only the exact recipient consumes, and
    only the generation this read returned; a newer one stays pending."""
    caller = caller or caller_session(root)
    entries = notices.outbox(d)
    current = next((e for e in entries.values() if e.get("generation") == notices.generation(d)), None)
    owner = recipient(current) if current is not None else None
    if owner is None:
        from .verdicts import may_clear_pending

        # No recipient: the legacy clear, by a reader allowed to read it.
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
    """What a read retires besides `verdict_read`: the launcher's own notices,
    exactly as `show` consumes them, and `chat_pending`, so the per-prompt fallback stops
    listing a return the adopting manager has read too."""
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
    """Every notice a delivery attempt may claim now, oldest first, one record read at a time."""
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
    """Supervisor start and collect: one bounded pass that retries pending or expired notices,
    oldest first. Consumed, acknowledged and superseded entries are never eligible."""
    from .terminal import recover_pending

    recover_pending(root, dispatch_ids, limit=limit)
    out: list[notice_delivery.Outcome] = []
    for item in candidates(root, dispatch_ids)[:limit]:
        outcome = deliver(root, item.dispatch_id, item.notice_id)
        if outcome is not None:
            out.append(outcome)
    return out
