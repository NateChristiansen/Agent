"""A dispatch's `cost_usd` counts each dollar once.

A provider total is the session's running total, and a forked session's total carries its
parent's. Each attempt adds only the rise over its baseline. A record from before that rule is
recomputed once, or marked unresolved and never refused on cost.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, features
from agent_cli.core import AgentError
from agent_cli.dispatch import spend
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- droot is a pytest fixture

# The package re-exports a function named `evaluate`, which shadows the module attribute.
evaluate_mod = importlib.import_module("agent_cli.dispatch.evaluate")
terminal_mod = importlib.import_module("agent_cli.dispatch.terminal")


def _record(**over: Any) -> dict[str, Any]:
    base = {"attempt": 1, "session_id": "s1", "cost_usd": 0.0, "cost_attempts": {}, "thread": []}
    return base | {"cost_accounting": spend.N1} | over


def _provider(total: float, session: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"cost_usd": total, "cost_evidence": "provider"}
    if session:
        out["cost_session_id"] = session
    return out


ESTIMATE = {"cost_usd": 0.25, "cost_source": "local-estimate", "cost_evidence": "estimate"}


def _run(d: dict[str, Any], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    for n, outcome in enumerate(outcomes, start=1):
        d["attempt"] = n
        spend.add_attempt_cost(d, outcome)
    return d


def test_same_session_resumes_add_only_the_rise():
    d = _run(_record(), [_provider(6.16), _provider(7.62), _provider(8.01)])
    assert d["cost_usd"] == pytest.approx(8.01)
    assert [d["cost_attempts"][k]["added"] for k in "123"] == pytest.approx([6.16, 1.46, 0.39])


def test_a_total_below_its_baseline_is_added_whole_and_noted():
    d = _run(_record(), [_provider(8.01), _provider(2.00)])
    assert d["cost_usd"] == pytest.approx(10.01)
    assert d["cost_attempts"]["2"]["added"] == pytest.approx(2.00)
    assert d["cost_attempts"]["2"]["note"] == "cost reset"
    assert "note" not in d["cost_attempts"]["1"]


def test_a_new_session_id_starts_from_zero():
    d = _run(_record(), [_provider(5.0), _provider(3.0, session="s2"), _provider(4.0, session="s2")])
    assert d["cost_attempts"]["2"]["baseline"] == 0.0
    assert "note" not in d["cost_attempts"]["2"]
    assert d["cost_usd"] == pytest.approx(5.0 + 3.0 + 1.0)


def test_a_forked_first_attempt_starts_from_the_parent_total():
    d = _record(forked_from_session="parent", fork_parent_cost_usd=8.98)
    _run(d, [_provider(10.70)])
    assert d["cost_usd"] == pytest.approx(1.72)
    assert d["cost_attempts"]["1"]["baseline"] == 8.98


def test_a_local_estimate_between_reported_attempts_adds_once_and_sets_no_baseline():
    d = _run(_record(), [_provider(1.0), ESTIMATE, _provider(1.5)])
    assert d["cost_usd"] == pytest.approx(1.5)
    assert d["cost_estimated_attempts"] == [2]
    assert "2" not in d["cost_attempts"]
    assert d["cost_attempts"]["3"]["added"] == pytest.approx(0.25)
    assert d["cost_attempts"]["3"]["estimate_credit"] == pytest.approx(0.25)
    assert d["cost_attempts"]["3"]["baseline"] == 1.0
    assert spend.last_reported_total(d, "s1") == 1.5


@pytest.mark.parametrize("total", [0.1, 0.25, 1.0])
def test_first_provider_total_replaces_same_session_estimates(total: float):
    d = _run(_record(), [ESTIMATE, ESTIMATE])
    assert d["cost_usd"] == 0.5
    assert spend.last_reported_total(d, "s1") is None
    d["attempt"] = 3
    spend.add_attempt_cost(d, _provider(total))
    assert d["cost_usd"] == pytest.approx(total)
    assert d["cost_attempts"]["3"]["added"] == pytest.approx(total - 0.5)
    d["attempt"] = 4
    spend.add_attempt_cost(d, _provider(total + 0.2))
    assert d["cost_usd"] == pytest.approx(total + 0.2)


def test_fresh_session_cannot_reconcile_another_sessions_estimate():
    d = _run(_record(), [ESTIMATE, _provider(1.0, "s2"), _provider(1.5, "s2")])
    assert d["cost_usd"] == pytest.approx(1.75)
    d["attempt"] = 4
    spend.add_attempt_cost(d, _provider(0.5, "s1"))
    assert d["cost_usd"] == pytest.approx(2.0)


def test_forked_estimate_is_reconciled_against_parent_baseline():
    d = _run(
        _record(forked_from_session="parent", fork_parent_cost_usd=8.98),
        [ESTIMATE, _provider(10.70), _provider(11.0)],
    )
    assert d["cost_usd"] == pytest.approx(2.02)
    assert d["cost_attempts"]["2"]["baseline"] == 8.98


def test_reset_keeps_estimates_before_reset_and_reconciles_only_later_estimates():
    d = _run(_record(), [_provider(5.0), ESTIMATE, _provider(1.0), ESTIMATE, _provider(1.5)])
    assert d["cost_usd"] == pytest.approx(6.75)
    assert d["cost_attempts"]["3"]["note"] == "cost reset"
    assert "estimate_credit" not in d["cost_attempts"]["3"]
    assert d["cost_attempts"]["5"]["estimate_credit"] == 0.25


def test_unattributed_old_estimate_is_not_subtracted_from_a_provider_total():
    d = _record(attempt=2, cost_usd=0.25, cost_estimated_attempts=[1])
    spend.add_attempt_cost(d, _provider(1.0))
    assert d["cost_usd"] == 1.25


def test_legacy_estimates_without_session_evidence_stay_unresolved(tmp_path: Path):
    d = {
        "attempt": 2,
        "session_id": "s1",
        "cost_usd": 0.25,
        "cost_estimated_attempts": [1],
        "thread": [{"from": "worker", "attempt": 1, "cost_usd": 0.25}],
        "budget_usd": 0.1,
    }
    spend.add_attempt_cost(d, _provider(1.0))
    assert d["cost_accounting"] == spend.UNRESOLVED
    assert spend.sdk_budget(d, tmp_path) == 0.1


def test_a_legacy_record_is_recomputed_once_from_its_thread():
    """A record from before the rule has no `cost_accounting`; its worker thread lines hold the
    reported totals. Its whole history is recomputed on first touch, then it counts normally."""
    d = {
        "attempt": 3,
        "session_id": "s1",
        "cost_usd": 13.78,
        "thread": [
            {"from": "worker", "attempt": 1, "cost_usd": 6.16},
            {"from": "worker", "attempt": 2, "cost_usd": 7.62},
        ],
    }
    assert spend.add_attempt_cost(d, _provider(8.01)) == pytest.approx(0.39)
    assert d["cost_accounting"] == spend.NORMALIZED
    assert d["cost_usd_recorded_legacy"] == pytest.approx(13.78)
    assert d["cost_usd"] == pytest.approx(8.01)


@pytest.mark.parametrize(
    "totals,expected",
    [([5.831, 10.277, 12.008], 12.008), ([28.861, 19.554], 48.415), ([0.264, 0.295], 0.295)],
)
def test_real_legacy_shapes_normalize(totals: list[float], expected: float):
    thread = [{"from": "worker", "attempt": n, "cost_usd": t} for n, t in enumerate(totals, start=1)]
    d = {"attempt": len(totals), "session_id": "s1", "cost_usd": sum(totals), "thread": thread}
    assert spend.ensure_accounting(d, completed_through=len(totals), root=None) == spend.NORMALIZED
    assert d["cost_usd"] == pytest.approx(expected)


@pytest.mark.parametrize("estimated", [[], [1, 2, 3]])
def test_codex_per_attempt_figures_stay_a_sum(estimated):
    thread = [{"from": "worker", "attempt": n, "cost_usd": t} for n, t in enumerate([0.052, 0.025, 0.03], 1)]
    d = {"attempt": 3, "provider": "codex", "session_id": "s1", "cost_usd": 0.107, "thread": thread}
    d["cost_estimated_attempts"] = estimated
    spend.ensure_accounting(d, completed_through=3, root=None)
    assert d["cost_usd"] == pytest.approx(0.107)


def test_a_legacy_fork_without_its_parent_is_unresolved_and_never_refused(tmp_path: Path):
    thread = [{"from": "worker", "attempt": 1, "cost_usd": 10.7}]
    d = {
        "id": "001-mac-02",
        "attempt": 1,
        "session_id": "s1",
        "forked_from_session": "parent",
        "escalated_from": "001-mac-01",
        "budget_usd": 1.0,
        "cost_usd": 10.7,
        "thread": thread,
    }
    spend.require_budget_left(d, tmp_path)  # unresolved: no refusal
    assert d["cost_accounting"] == spend.UNRESOLVED
    for n, total in ((2, 11.0), (3, 11.5)):
        d["attempt"] = n
        spend.add_attempt_cost(d, _provider(total), tmp_path)
        assert d["cost_accounting"] == spend.UNRESOLVED
        spend.require_budget_left(d, tmp_path)
    assert d["cost_usd_since_unresolved"] == pytest.approx(0.8)  # 11.0 - 10.7, then 11.5 - 11.0
    assert d["cost_usd"] == pytest.approx(11.5)
    assert spend.sdk_budget(d, tmp_path) == 1.0
    assert d["cost_accounting"] == spend.UNRESOLVED


def test_a_legacy_fork_with_its_fork_parent_figure_normalizes():
    thread = [{"from": "worker", "attempt": 1, "cost_usd": 10.7}]
    d = {
        "attempt": 1,
        "session_id": "s1",
        "forked_from_session": "p",
        "fork_parent_cost_usd": 8.98,
        "cost_usd": 10.7,
        "thread": thread,
    }
    spend.ensure_accounting(d, completed_through=1, root=None)
    assert d["cost_accounting"] == spend.NORMALIZED and d["cost_usd"] == pytest.approx(1.72)


def test_cost_attempts_without_a_state_are_classified_by_first_touch_not_trusted():
    d = {
        "attempt": 2,
        "session_id": "s1",
        "cost_usd": 20.0,
        "cost_attempts": {"1": {"session_id": "s1", "total": 1.0, "added": 1.0}},
        "thread": [
            {"from": "worker", "attempt": 1, "cost_usd": 6.0},
            {"from": "worker", "attempt": 2, "cost_usd": 7.0},
        ],
    }
    assert spend.ensure_accounting(d, completed_through=2, root=None) == spend.NORMALIZED
    assert d["cost_usd"] == pytest.approx(7.0)


def test_n1_and_normalized_records_are_refused_once_spent_through_several_resumes():
    d = _record(id="001-mac-01", budget_usd=2.0)
    for n, total in ((1, 0.9), (2, 1.5), (3, 2.1)):
        d["attempt"] = n
        spend.add_attempt_cost(d, _provider(total))
    assert d["cost_usd"] == pytest.approx(2.1)
    with pytest.raises(AgentError, match=r"has spent \$2\.10 of its \$2\.00 budget"):
        spend.require_budget_left(d, Path("."))
    d["budget_usd"] = 3.0
    spend.require_budget_left(d, Path("."))


def test_last_reported_total_reads_entries_then_a_legacy_result():
    d = _run(_record(), [_provider(2.0), _provider(3.0)])
    assert spend.last_reported_total(d, "s1") == 3.0
    assert spend.last_reported_total(d, "other") is None
    legacy = {"session_id": "s1", "last_result": {"type": "result", "total_cost_usd": 8.98}}
    assert spend.last_reported_total(legacy, "s1") == 8.98
    codex = {"session_id": "s1", "last_result": {"total_cost_usd": 1.0, "cost_source": "local-estimate"}}
    assert spend.last_reported_total(codex, "s1") is None


def _fresh(root: Path, fake_claude) -> dict[str, Any]:
    fake_claude.scenario([one(envelope())])
    d = dispatch.load(root, _new_fast(root)["id"])
    for key in ("cost_evidence", "cost_estimated_attempts", "cost_accounted_attempt"):
        d.pop(key, None)
    d.update(attempt=1, cost_usd=0.0, cost_attempts={}, thread=[])
    return d


def test_timeout_auto_resume_then_finish_counts_each_dollar_once(droot: Path, fake_claude):
    d = _fresh(droot, fake_claude)
    feat = features.resolve(droot, str(d["feature"]))
    d = evaluate_mod.queue_auto_resume(droot, d, feat, {"state": "blocked", **_provider(6.16)}, "timeout")
    assert d["attempt"] == 2 and d["cost_usd"] == pytest.approx(6.16)
    terminal_mod._account(d, _provider(7.62))
    assert d["cost_usd"] == pytest.approx(7.62)
    terminal_mod._account(d, _provider(7.62))
    assert d["cost_usd"] == pytest.approx(7.62)
    worker = [t for t in d["thread"] if t.get("from") == "worker"]
    assert worker[0]["cost_usd"] == pytest.approx(6.16)


def test_timeout_estimate_then_provider_result_reconciles_once(droot: Path, fake_claude):
    d = _fresh(droot, fake_claude)
    feat = features.resolve(droot, str(d["feature"]))
    d = evaluate_mod.queue_auto_resume(droot, d, feat, {"state": "blocked", **ESTIMATE}, "timeout")
    assert d["attempt"] == 2 and d["cost_usd"] == 0.25
    assert spend.sdk_budget(d, droot) == 0.75
    terminal_mod._account(d, _provider(0.6))
    terminal_mod._account(d, _provider(0.6))
    assert d["cost_usd"] == pytest.approx(0.6)
    assert d["cost_evidence"] == {"1": "estimate", "2": "provider"}


def test_resumed_dispatch_records_its_own_spend(droot: Path, fake_claude):
    fake_claude.scenario(
        [one(envelope("question", open_questions=["which port?"], cost=6.16)), one(envelope(cost=7.62))]
    )
    d = _new_fast(droot)
    d = dispatch.resume(
        droot,
        dispatch_id=d["id"],
        answer="use 8080",
        watch=True,
        budget=10.0,
        budget_reason="room for the fix",
    )
    assert d["attempt"] == 2 and d["cost_usd"] == pytest.approx(7.62)
    worker = [t["cost_usd"] for t in d["thread"] if t.get("from") == "worker"]
    assert worker == pytest.approx([6.16, 1.46])


def test_an_escalation_carries_the_parent_total_into_the_fork(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("blocked", cost=8.98)), one(envelope(cost=10.70))])
    old = _new_fast(droot)
    fork = _new_fast(droot, escalate_from=old["id"])
    assert fork["forked_from_session"] == old["session_id"]
    assert fork["fork_parent_cost_usd"] == pytest.approx(8.98)
    assert fork["cost_usd"] == pytest.approx(1.72)
