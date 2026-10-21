"""What one attempt spent, when the provider reports a running total for its session.

Claude reports `total_cost_usd` for the whole session, and a forked session's total carries
its parent's. Adding each attempt's figure counted earlier attempts again: a record showed a
cost far above the work done. Each provider-reported attempt keeps its
session id and total in `cost_attempts`, keyed by attempt, and adds only the rise over the last
total that session reported. A forked session starts from its parent's last total, carried on
the record as `fork_parent_cost_usd`. A local estimate is added whole until the same session
reports a total covering it. Estimates stay separate from provider baselines. A total below
its baseline is a reset: added whole and noted, retaining preceding estimates.

Every record carries an explicit `cost_accounting` state, and only this module writes it:

- `n1`: created under this rule.
- `normalized-legacy`: a record from before it, whose whole history was recomputed under the
  rule the first time it was touched. The original sum stays as `cost_usd_recorded_legacy`.
- `legacy-unresolved`: a record from before it whose history cannot be recomputed (a fork
  whose parent is gone, an attempt with no figure). It keeps adding correct deltas, but its
  `cost_usd` still holds the inflated history, so it is never refused on cost. The state is
  sticky: a later attempt never makes the history trustworthy.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from ..core import AgentError
from ..records.dispatch import thread_of
from ..records.narrow import as_dict, as_dict_or_none, as_float, dig

N1, NORMALIZED, UNRESOLVED = "n1", "normalized-legacy", "legacy-unresolved"
STATES = (N1, NORMALIZED, UNRESOLVED)


def add_attempt_cost(d: dict[str, Any], outcome: dict[str, Any], root: Path | None = None) -> float:
    """Add what `d`'s current attempt spent to `d["cost_usd"]` and return it. Callers hold the
    duplicate-finalize guard; this adds every time it is called."""
    attempt = int(d["attempt"])
    ensure_accounting(d, completed_through=attempt - 1, root=root)
    reported = float(outcome.get("cost_usd") or 0)
    added = reported
    session = str(outcome.get("cost_session_id") or d.get("session_id") or "")
    if outcome.get("cost_source") == "local-estimate":
        d.setdefault("cost_estimated_attempts", []).append(attempt)
        d.setdefault("cost_estimates", {})[str(attempt)] = {"session_id": session, "amount": reported}
    elif outcome.get("cost_evidence") == "provider":
        baseline = _baseline(d, session)
        entry: dict[str, Any] = {"session_id": session, "total": reported, "baseline": baseline}
        if reported >= baseline:
            added = reported - baseline
            credit = _estimated_since_report(d, session)
            if credit:
                entry["estimate_credit"] = credit
                added -= credit
        else:
            entry["note"] = "cost reset"
        entry["added"] = added
        attempts = d.get("cost_attempts")
        if not isinstance(attempts, dict):
            attempts = {}
        attempts[str(attempt)] = entry
        d["cost_attempts"] = attempts
    d["cost_usd"] = float(d.get("cost_usd") or 0) + added
    if d["cost_accounting"] == UNRESOLVED:
        d["cost_usd_since_unresolved"] = float(d.get("cost_usd_since_unresolved") or 0) + added
    return added


def attempt_cost(d: dict[str, Any], attempt: int, fallback: Any) -> Any:
    """What `attempt` added, for the thread line; `fallback` when it has no provider entry."""
    entry = as_dict(dig(d, "cost_attempts", str(attempt)))
    return entry["added"] if "added" in entry else fallback


def last_reported_total(d: dict[str, Any], session: str) -> float | None:
    """The last provider total `session` reported on record `d`, or None. A record from before
    `cost_attempts` falls back to its last result when that result is a provider total."""
    attempts = as_dict_or_none(d.get("cost_attempts"))
    if attempts is not None:
        found = _latest(attempts, session, before=None)
        if found is not None:
            return found
    result = as_dict_or_none(d.get("last_result"))
    if (
        result is not None
        and result.get("total_cost_usd") is not None
        and result.get("cost_source") is None
        and (result.get("session_id") or d.get("session_id")) == session
    ):
        return as_float(result["total_cost_usd"])
    return None


def parent_total(old: dict[str, Any] | None, session: str | None) -> float | None:
    """The fork's starting point: the parent's last reported total for the forked session."""
    return last_reported_total(old, session) if old and session else None


def ensure_accounting(d: dict[str, Any], *, completed_through: int, root: Path | None) -> str:
    """`d`'s accounting state, deciding it once for a record from before the rule: its first
    `completed_through` attempts are recomputed, or the record is marked unresolved."""
    state = d.get("cost_accounting")
    if state in STATES:
        return str(state)
    normalized = _normalize(d, completed_through, root)
    if normalized is None:
        d["cost_accounting"] = UNRESOLVED
        d["cost_usd_since_unresolved"] = 0.0
    else:
        total, entries = normalized
        d["cost_usd_recorded_legacy"] = float(d.get("cost_usd") or 0)
        d["cost_usd"] = total
        d["cost_attempts"] = entries
        d["cost_accounting"] = NORMALIZED
    return str(d["cost_accounting"])


def require_budget_left(d: dict[str, Any], root: Path) -> None:
    """Refuse a resume whose dispatch has already spent its whole budget. The budget is the total
    across attempts, as `cost_usd` is. A record whose history is unresolved is never refused."""
    if ensure_accounting(d, completed_through=int(d.get("attempt") or 1), root=root) == UNRESOLVED:
        return
    spent, budget = float(d.get("cost_usd") or 0), float(d["budget_usd"])
    if spent >= budget:
        raise AgentError(
            f"dispatch {d['id']} has spent ${spent:.2f} of its ${budget:.2f} budget, which is the total "
            "across attempts; resume with a `budget_usd` above what is spent and a `budget_reason`"
        )


def sdk_budget(d: dict[str, Any], root: Path) -> float:
    """Cap an upcoming attempt at the remaining trustworthy dispatch budget."""
    state = ensure_accounting(d, completed_through=int(d.get("attempt") or 1) - 1, root=root)
    if state == UNRESOLVED:
        return float(d["budget_usd"])
    require_budget_left(d, root)
    return max(0.0, float(d["budget_usd"]) - float(d.get("cost_usd") or 0))


def _normalize(d: dict[str, Any], through: int, root: Path | None) -> tuple[float, dict[str, Any]] | None:
    """Recompute a legacy record's first `through` attempts from its worker thread lines, which
    carried each attempt's reported figure. None when that cannot be done honestly."""
    lines: dict[int, Any] = {}
    for line in thread_of(d):
        n = line.get("attempt")
        if line.get("from") == "worker" and isinstance(n, int) and 1 <= n <= through:
            lines[n] = line.get("cost_usd")
    if any(lines.get(n) is None for n in range(1, through + 1)):
        return None
    estimated = set(d.get("cost_estimated_attempts") or [])
    whole = str(d.get("provider") or "claude") == "codex"  # Codex figures are per attempt
    if estimated and not whole:
        # Old estimates lack session attribution, so later totals cannot reconcile them safely.
        return None
    session = str(d.get("session_id") or "")
    baseline = 0.0
    if d.get("forked_from_session") and through >= 1 and not whole:
        parent = _fork_parent_total(d, root)
        if parent is None:
            return None
        baseline = parent
    total = 0.0
    entries: dict[str, Any] = {}
    for n in range(1, through + 1):
        reported = float(lines[n])
        if whole or n in estimated:
            total += reported
            continue
        entry: dict[str, Any] = {
            "session_id": session,
            "total": reported,
            "baseline": baseline,
            "source": NORMALIZED,
        }
        added = reported - baseline if reported >= baseline else reported
        if reported < baseline:
            entry["note"] = "cost reset"
        entry["added"] = added
        entries[str(n)] = entry
        total += added
        baseline = reported
    return total, entries


def _fork_parent_total(d: dict[str, Any], root: Path | None) -> float | None:
    if d.get("fork_parent_cost_usd") is not None:
        return float(d["fork_parent_cost_usd"])
    parent_id, session = d.get("escalated_from"), str(d.get("forked_from_session") or "")
    if not parent_id or root is None:
        return None
    from .state import load

    try:
        parent = load(root, str(parent_id))
    except Exception:  # noqa: BLE001 — a missing parent leaves the history unresolved
        return None
    return last_reported_total(parent, session) if "cost_attempts" in parent else _legacy_last(parent)


def _legacy_last(parent: dict[str, Any]) -> float | None:
    for entry in reversed(parent.get("thread") or []):
        if entry.get("from") == "worker" and entry.get("cost_usd") is not None:
            return float(entry["cost_usd"])
    return None


def _baseline(d: dict[str, Any], session: str) -> float:
    attempt = int(d["attempt"])
    found = _latest(d.get("cost_attempts") or {}, session, before=attempt)
    if found is not None:
        return found
    if d.get("cost_accounting") == UNRESOLVED and session == d.get("session_id"):
        # An unresolved record's earlier attempts are only in its thread, as reported totals.
        legacy = _legacy_thread_total(d, attempt)
        if legacy is not None:
            return legacy
    if session == d.get("session_id") and d.get("forked_from_session"):
        return float(d.get("fork_parent_cost_usd") or 0)
    return 0.0


def _latest(attempts: dict[str, Any], session: str, before: int | None) -> float | None:
    best: tuple[int, float] | None = None
    for key, value in attempts.items():
        entry = as_dict_or_none(value)
        if entry is None or entry.get("session_id") != session:
            continue
        n = int(key)
        if before is not None and n >= before:
            continue
        if best is None or n > best[0]:
            best = (n, as_float(entry.get("total")))
    return best[1] if best is not None else None


def _estimated_since_report(d: dict[str, Any], session: str) -> float:
    attempt = int(d["attempt"])
    previous = max(
        (
            int(n)
            for n, entry in as_dict(d.get("cost_attempts")).items()
            if as_dict(entry).get("session_id") == session and int(n) < attempt
        ),
        default=0,
    )
    return sum(
        as_float(as_dict(entry).get("amount"))
        for n, entry in as_dict(d.get("cost_estimates")).items()
        if previous < int(n) < attempt and as_dict(entry).get("session_id") == session and session
    )


def _legacy_thread_total(d: dict[str, Any], attempt: int) -> float | None:
    estimated = set(d.get("cost_estimated_attempts") or [])
    for entry in reversed(d.get("thread") or []):
        n = entry.get("attempt")
        if entry.get("from") != "worker" or not isinstance(n, int) or n >= attempt or n in estimated:
            continue
        return None if entry.get("cost_usd") is None else float(entry["cost_usd"])
    return None


CostSource = Literal["provider-result", "local-estimate", "legacy-recorded", "unknown"]


def current_cost(raw: dict[str, Any]) -> tuple[float | None, CostSource]:
    """A record's cost and the evidence behind it, for the ledger, journal, usage and report."""
    amount = _money(raw.get("cost_usd"))
    if amount is not None and raw.get("cost_estimated_attempts"):
        return amount, "local-estimate"
    result = as_dict_or_none(raw.get("last_result"))
    reported = None
    if result is not None and result.get("type") == "result":
        reported = _money(result.get("total_cost_usd"))
    if amount is not None and raw.get("attempt") == 1 and reported == amount:
        return amount, "provider-result"
    if amount is not None and _provider_attempts_cover(raw, amount):
        return amount, "provider-result"
    if amount is not None and amount > 0:
        return amount, "legacy-recorded"
    return None, "unknown"


def _provider_attempts_cover(raw: dict[str, Any], amount: float) -> bool:
    """Whether every attempt has a provider entry in `cost_attempts` and their added amounts sum
    to `amount`: a resumed record measured under the per-session baseline rule."""
    attempts = as_dict_or_none(raw.get("cost_attempts"))
    count = raw.get("attempt")
    if attempts is None or type(count) is not int or count < 1:
        return False
    added: list[float] = []
    for n in range(1, count + 1):
        amount_added = dig(attempts, str(n), "added")
        if not isinstance(amount_added, (int, float)):
            return False
        added.append(float(amount_added))
    return math.isclose(sum(added), amount, abs_tol=1e-6)


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentError("cost must be null or a finite nonnegative number")
    try:
        amount = float(value)
    except OverflowError as exc:
        raise AgentError("cost must be finite") from exc
    if not math.isfinite(amount) or amount < 0:
        raise AgentError("cost must be null or a finite nonnegative number")
    return 0.0 if amount == 0 else amount
