"""One guarded terminal commit and its concurrent-caller side effects.

The record commit is durable; external effects are not a crash-safe exactly-once outbox.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .. import features as features_mod
from .. import log as log_mod
from ..core import AgentError, atomic_write, iso, now
from ..records.dispatch import result_of
from ..records.narrow import as_list
from . import holdings, notice_producers, telemetry_events
from . import verification_gate as gate
from .handoffs import handoff_log_text, transcript_path, write_handoff
from .notify import notify
from .spend import add_attempt_cost, attempt_cost
from .state import (
    TERMINAL_STATES,
    dispatches_dir,
    file,
    load,
    one_line,
    release_repo_lease,
    render,
    save,
    shown_denials,
    store_result,
)
from .telemetry_events import note_cost_evidence
from .verification_gate import TerminalBusy, acceptance, requires_lease, terminal_guard


def finalize(
    root: Path,
    d: dict[str, Any],
    feat: features_mod.Feature,
    outcome: dict[str, Any],
    *,
    retry_budget: float | None = None,
    retain: bool = True,
    keep_intent: bool = False,
) -> dict[str, Any]:
    """Only `recover_pending` passes `retain=False` and `keep_intent=True`: it removes the
    intent itself once the transcripts the intent owes are queued."""
    try:
        try:
            with notice_producers.delivering(root, d["id"]) as sent:
                current, committed, assessed = _commit(root, d, outcome, sent, retry_budget)
                if committed:
                    _publish(root, current, feat, assessed)
                elif current.get("verification_cleanup_pending"):
                    write_handoff(root, current, feat)
                    render(root)
        except TerminalBusy:
            if keep_intent:
                raise
            return _defer(root, d, outcome)
        if not keep_intent:
            pending_path(root, d).unlink(missing_ok=True)
        return load(root, current["id"])
    finally:
        # Every route a stopped worker takes (commit, cleanup pending, a deferred or failed
        # publication, a cancel, a recovery) queues its transcripts, whether or not a manager
        # Stop or a closure ever comes. A surviving publication intent is the durable
        # obligation; with none, a failed enqueue leaves a marker.
        if retain:
            intent = pending_path(root, d)
            if retain_transcripts(root, d, marker=not intent.exists()):
                _mark_retained(intent)


def retention_record(d: dict[str, Any]) -> dict[str, Any]:
    """The worker's native transcript references in `transcript_retention.enqueue`'s shape. A Codex
    rollout and its spawned threads are found by the native thread id, never by the dispatch's
    synthetic `session_id`; with no thread id recorded the retention report says so."""
    record: dict[str, Any] = {"feature": d.get("feature"), "dispatch_id": d.get("id")}
    if d.get("provider") == "codex":
        return {**record, "session_id": d.get("codex_thread_id") or None, "transcript_path": None}
    path = transcript_path(d)
    return {**record, "session_id": d.get("session_id"), "transcript_path": str(path) if path else None}


def retain_transcripts(root: Path, d: dict[str, Any], *, marker: bool = True) -> bool:
    """Queue the worker's transcripts for detached, bounded retention; nothing is copied here.
    Fails open: retention never fails or delays publication, and a request already spooled
    survives a consumer that did not start. True when the work is queued. A failure before
    anything is spooled leaves a marker (unless `marker` is False because a publication intent
    already owes it) that `recover_pending` retries until the work is queued."""
    from ..hooks import transcript_retention

    mark = retention_marker(root, d)
    try:
        current = load(root, d["id"])
    except AgentError, OSError, ValueError:
        current = d
    started = time.time_ns() - 2_000_000_000  # coarse file timestamps
    try:
        transcript_retention.enqueue(root, retention_record(current))
    except Exception as exc:  # noqa: BLE001 — retention is best effort beside the terminal commit
        sys.stderr.write(f"note: transcript retention not queued for {d.get('id')} ({exc})\n")
        if _spooled(root, d["id"], started):
            mark.unlink(missing_ok=True)
            return True
        if marker:
            try:
                atomic_write(mark, json.dumps({"dispatch": d["id"]}) + "\n")
            except OSError as marker_exc:
                sys.stderr.write(f"note: retention retry not recorded for {d.get('id')} ({marker_exc})\n")
        return False
    mark.unlink(missing_ok=True)
    return True


def retention_marker(root: Path, d: dict[str, Any]) -> Path:
    return file(root, d["id"], ".retention.pending")


SPOOL_PROBE_READS = 4


def _spooled(root: Path, dispatch_id: str, since_ns: int) -> bool:
    """Whether this attempt's enqueue reached the spool before failing. Looks only at the few
    newest entries written since it began, so the cost does not grow with the queue."""
    from ..hooks import transcript_retention

    try:
        with os.scandir(root / transcript_retention.BASE / "pending") as entries:
            fresh = sorted(
                (e for e in entries if e.name.endswith(".json") and e.stat().st_mtime_ns >= since_ns),
                key=lambda e: e.stat().st_mtime_ns,
                reverse=True,
            )
        return any(
            transcript_retention.load(Path(e.path)).get("dispatch_id") == dispatch_id
            for e in fresh[:SPOOL_PROBE_READS]
        )
    except OSError, ValueError, AttributeError:
        return False


def _mark_retained(intent: Path) -> None:
    try:
        saved = json.loads(intent.read_text(encoding="utf-8"))
        saved["retention"] = "queued"
        atomic_write(intent, json.dumps(saved, default=str) + "\n")
    except OSError, ValueError:
        return


def _commit(
    root: Path,
    d: dict[str, Any],
    outcome: dict[str, Any],
    sent: list[str],
    budget: float | None,
) -> tuple[dict[str, Any], bool, str | None]:
    deadline = time.monotonic() + (gate.TERMINAL_RETRY_SECONDS if budget is None else budget)
    while True:
        observed = load(root, d["id"])
        if observed["attempt"] != d["attempt"] or observed.get("terminal_committed"):
            return observed, False, None
        # Historical terminal records need an explicit pending outcome before acceptance.
        if (
            observed["state"] in TERMINAL_STATES
            and observed["state"] != "cancelled"
            and not observed.get("pending_outcome")
        ):
            return observed, False, None
        revision = observed.get("record_revision", 0)
        prepared, committed, assessed = _prepare(root, observed, outcome)
        with terminal_guard(root, d["id"], budget=max(0, deadline - time.monotonic())) as current:
            if current.get("record_revision", 0) != revision:
                if time.monotonic() >= deadline:
                    raise TerminalBusy("terminal record kept changing during publication")
                continue
            if committed:
                notice_producers.terminal(root, prepared, sent)
            else:
                notice_producers.cleanup_incomplete(root, prepared, sent)
            save(root, prepared)
            return prepared, committed, assessed


def _prepare(
    root: Path,
    current: dict[str, Any],
    outcome: dict[str, Any],
) -> tuple[dict[str, Any], bool, str | None]:
    """Assess outside the record lock; commit only while the observed revision still matches."""
    outcome = dict(outcome)
    if "permission_denials" in outcome:
        # a `last_result` persisted before denials were shown arrives raw
        outcome["permission_denials"] = shown_denials(outcome["permission_denials"])
    if current["state"] == "cancelled":
        unknown: object = current.get("cancel_unverified_pids") or []
        inventory_error = current.get("cancel_inventory_error") or ""
        detail = inventory_error or f"unverified PIDs {unknown}"
        error = f"cancel incomplete: {detail}" if unknown or inventory_error else "cancelled by the manager"
        outcome.update(state="cancelled", error=error, auto_resume=None)
    if requires_lease(root, current):
        current.update(verification_cleanup_pending=True, terminal_committed=False)
        if outcome["state"] != "done":
            # The return is kept once, in `result`; `pending_outcome_of` puts it back.
            current["pending_outcome"] = {k: v for k, v in outcome.items() if k != "result"}
            store_result(current, outcome.get("result"), returned=bool(outcome.get("returned")))
            current.update(
                state=outcome["state"],
                error=outcome.get("error")
                or "verification or worker cleanup is incomplete; collect again after cleanup",
            )
            _account(current, outcome, root)
        else:
            current["error"] = "verification or worker cleanup is incomplete; collect again after cleanup"
        return current, False, None
    assessed: str | None = None
    if outcome["state"] == "done":
        try:
            holdings.hold(root, current)
        except AgentError:
            outcome.update(
                state="blocked",
                error="worktree or repo slot lease changed before acceptance; verification not accepted",
            )
    if outcome["state"] == "done":
        assessment = acceptance(root, current)
        failures = failing_tests(root, current, assessment.status)
        current["verification_result"] = {
            "status": assessment.status,
            "reasons": list(assessment.reasons) + failures,
        }
        assessed = assessment.status
        if assessment.infrastructure:
            result = dict(outcome.get("result") or {})
            result["notes"] = (
                str(result.get("notes") or "")
                + "\n[supervisor] WARNING: verification incomplete; "
                + "; ".join(r.removeprefix("WARNING: ") for r in assessment.reasons)
            ).strip()
            outcome["result"] = result
        elif assessment.status not in ("verified", "exempt"):
            outcome.update(state="blocked", error=blocked_reason(list(assessment.reasons), failures))
        elif assessment.status == "exempt":
            result = dict(outcome.get("result") or {})
            result["notes"] = (
                str(result.get("notes") or "")
                + "\n[supervisor] verification exempt: "
                + "; ".join(r.removeprefix("exemption: ") for r in assessment.reasons)
            ).strip()
            outcome["result"] = result
    _account(current, outcome, root)
    current.pop("pending_outcome", None)
    store_result(current, outcome.get("result"), returned=bool(outcome.get("returned")))
    current.update(
        state=outcome["state"],
        error=outcome.get("error"),
        finished=iso(now()),
        terminal_committed=True,
        verification_cleanup_pending=False,
    )
    result = result_of(current)
    current["thread"].append(
        {
            "ts": iso(now()),
            "from": "worker",
            "text": one_line(result.get("summary") or current.get("error") or "(no return)"),
            "status": current["state"],
            "attempt": current["attempt"],
            "cost_usd": attempt_cost(current, current["attempt"], outcome.get("cost_usd")),
        }
    )
    return current, True, assessed


def _publish(root: Path, current: dict[str, Any], feat: features_mod.Feature, assessed: str | None) -> None:
    from ..journal import pending as journal_pending
    from ..usage import job as usage_job

    telemetry_events.verification(root, current, assessed)
    usage_job.trigger_quietly(root, f"dispatch {current['id']} {current['state']}")
    telemetry_events.terminal(root, current)
    phase = {"question": "awaiting-answer", "done": "awaiting-landing"}.get(current["state"], "ended")
    telemetry_events.transition(root, current, phase, state=current["state"])
    write_handoff(root, current, feat)
    log_mod.append(feat, "handoff", handoff_log_text(root, current))
    release_repo_lease(root, current)
    render(root)
    notify(root, current, current["state"])
    journal_pending.record(
        root,
        "dispatch-return",
        f"{current['id']} {current['state']}",
        one_line(result_of(current).get("summary") or ""),
    )


def _defer(root: Path, d: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Keep the verified return and a notice intent without overwriting a busy record.

    The pending-notice reconciler retries publication before offering the notice, so a
    cancellation or newer attempt still fences it. No lease is released on this path.
    """
    current = load(root, d["id"])
    if current["attempt"] != d["attempt"] or current.get("terminal_committed"):
        return current
    intent = {**current, "state": outcome["state"]}
    if current["state"] == "cancelled":
        intent["state"] = "cancelled"
    if intent["state"] == "done":
        assessment = acceptance(root, current)
        intent["verification_result"] = {"status": assessment.status, "reasons": list(assessment.reasons)}
        if assessment.status not in ("verified", "exempt") and not assessment.infrastructure:
            intent["state"] = "blocked"
    # The snapshot is independent of the live record, including its mutable outbox.
    intent = json.loads(json.dumps(intent))
    store_result(intent, outcome.get("result"), returned=bool(outcome.get("returned")))
    notice_producers.terminal(root, intent, [])
    atomic_write(
        pending_path(root, d),
        json.dumps(
            {
                "dispatch": intent,
                "outcome": outcome,
                "retention": "owed",
            },
            default=str,
        )
        + "\n",
    )
    return current


def pending_path(root: Path, d: dict[str, Any]) -> Path:
    return file(root, d["id"], f".terminal-{int(d['attempt'])}.pending")


def recover_pending(root: Path, dispatch_ids: list[str] | None = None, *, limit: int = 3) -> None:
    """Retry durable publication intents as part of the pending-notice pass."""
    paths = (
        [path for did in dispatch_ids for path in dispatches_dir(root).glob(f"{did}.terminal-*.pending")][
            :limit
        ]
        if dispatch_ids is not None
        else sorted(dispatches_dir(root).glob("*.terminal-*.pending"))[:limit]
    )
    for path in paths:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            d = saved["dispatch"]
            feat = features_mod.resolve(root, str(d["feature"]))
            finalize(root, d, feat, saved["outcome"], retry_budget=0, retain=False, keep_intent=True)
            if saved.get("retention") != "queued" and not retain_transcripts(root, d, marker=False):
                continue
            path.unlink(missing_ok=True)
        except AgentError, OSError, ValueError, KeyError, TypeError:
            continue
    markers = (
        [path for did in dispatch_ids for path in dispatches_dir(root).glob(f"{did}.retention.pending")]
        if dispatch_ids is not None
        else sorted(dispatches_dir(root).glob("*.retention.pending"))
    )
    for path in markers[:limit]:
        try:
            retain_transcripts(root, load(root, path.name.removesuffix(".retention.pending")))
        except AgentError, OSError, ValueError, KeyError, TypeError:
            continue


def _account(current: dict[str, Any], outcome: dict[str, Any], root: Path | None = None) -> None:
    for key in ("git", "conventions", "terminal_reason", "legacy_denials"):
        if key in outcome:
            current[key] = outcome[key]
    if current.get("cost_accounted_attempt") == current["attempt"]:
        return
    add_attempt_cost(current, outcome, root)
    note_cost_evidence(current, outcome)
    current["num_turns"] = int(outcome.get("num_turns") or 0)
    if outcome.get("permission_denials"):
        current["permission_denials"] = shown_denials(
            as_list(current.get("permission_denials")) + as_list(outcome["permission_denials"])
        )
    current["cost_accounted_attempt"] = current["attempt"]


def blocked_reason(reasons: list[str], failures: list[str]) -> str:
    """Lead with failing test names and shorten long command reasons to their first words, keeping the
    actionable failure visible.
    """
    shown = [_shorten(r) for r in reasons]
    return "; ".join([*failures, *shown])


def _shorten(reason: str) -> str:
    start = reason.find("`")
    end = reason.find("`", start + 1)
    if start < 0 or end < 0 or end - start <= 160:
        return reason
    words = reason[start + 1 : end].split()
    command = " ".join(words[:4]) + f" … (+{len(words) - 4} more arguments)"
    return reason[: start + 1] + command + reason[end:]


FAILURE_LINES = 8
_FAILURE_RE = re.compile(r"^(FAILED |ERROR )\S+")


def failing_tests(root: Path, d: dict[str, Any], status: str) -> list[str]:
    """For a failed run, up to `FAILURE_LINES` of pytest's `FAILED`/`ERROR` summary lines from the
    run's log, so a manager sees which tests failed without opening it."""
    if status != "failed" or not d.get("run_area"):
        return []
    try:
        text = (root / str(d["run_area"]) / "output.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line[:200] for line in text.splitlines() if _FAILURE_RE.match(line)]
    return lines[:FAILURE_LINES] + (
        [f"... {len(lines) - FAILURE_LINES} more"] if len(lines) > FAILURE_LINES else []
    )
