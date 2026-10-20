"""Dispatch record readers on malformed values."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli import dispatch
from agent_cli import leases as leases_mod
from agent_cli import repos as repos_mod
from agent_cli.dispatch import handoffs, lane, prompt, spend, state, telemetry_events, usage
from agent_cli.dispatch.notify import notify, pending_block
from dispatch_helpers import malformed_record as _record

evaluate_mod = importlib.import_module("agent_cli.dispatch.evaluate")
GH_REPO = repos_mod.Repo(
    name="web-app", short="web", path="repos/web-app", remote="git@github.com:example-org/web-app.git"
)


@pytest.mark.parametrize("count", ["5", 5.0, True])
def test_a_token_count_that_is_not_an_int_counts_as_zero(count: object):
    assert usage.message_tokens({"input_tokens": count, "output_tokens": 2}) == 2


def test_a_provider_total_that_is_not_a_number_reads_as_zero():
    assert (
        spend.last_reported_total({"session_id": "s", "last_result": {"total_cost_usd": "1.5"}}, "s") == 0.0
    )
    assert spend.last_reported_total({"cost_attempts": {"1": {"session_id": "s", "total": "2"}}}, "s") == 0.0


def test_legacy_thread_lines_that_are_not_objects_are_skipped_when_recomputing_cost():
    d = {"session_id": "s", "thread": ["junk", {"from": "worker", "attempt": 1, "cost_usd": 0.5}]}
    normalized = spend._normalize(d, 1, None)
    assert normalized is not None and normalized[0] == 0.5


def test_a_thread_cost_that_is_not_a_number_shows_as_zero():
    entry = {"from": "worker", "ts": "t", "status": "done", "attempt": 1, "cost_usd": "1.5"}
    assert prompt.thread_heading(entry) == "t worker → manager (done, attempt 1, $0.00)"


def test_thread_entries_that_are_not_objects_are_skipped():
    d = _record(thread=["junk", {"from": "manager", "text": "do it"}])
    assert prompt.manager_prompt(d) == "do it"
    assert prompt.last_manager_message(d) == "do it"
    assert "do it" in prompt.compose_followup(d)
    assert state.prompt_text(Path("/nonexistent"), d) == "do it"


def test_findings_skip_reasons_and_next_steps_that_are_not_lists():
    d = _record(verification_result={"status": "failed", "reasons": "ab"}, result={"next_steps": "cd"})
    assert prompt._findings(d) == ["- verification failed: "]


def test_denials_that_are_not_a_list_and_a_result_that_is_not_an_object_list_nothing():
    assert state.permission_denials(_record(permission_denials="ab", result="junk")) == []


def test_a_result_that_is_not_an_object_summarizes_from_the_error():
    assert state.summarize(_record(result="junk", error="boom"))["summary"] == "boom"


def test_handoff_text_skips_a_malformed_result_and_auto_resume_rows():
    d = _record(
        num_turns=3,
        result="junk",
        auto_resumes=["junk", {"reason": "timeout", "from_budget": "5", "to_budget": 5}],
    )
    assert "timeout (budget $0.00 → $5.00)" in handoffs.handoff_log_text(Path("."), d)
    built = {"commits": [{"sha": "a", "subject": "s"}], "files": ["a.py"]}
    assert handoffs.handoff_ask(_record(result="junk", git=built)) == "review, then push or ack"


def test_each_dispatch_kind_gets_its_own_next_action():
    """ "review, then push" only for a branch with a diff."""
    built = {"commits": [{"sha": "a", "subject": "s"}], "files": ["a.py"]}
    assert handoffs.handoff_ask(_record(git=built)) == "review, then push or ack"
    assert str(handoffs.handoff_ask(_record(git={"commits": [], "files": []}))).startswith("no changes")
    assert "dispatch consume" in str(handoffs.handoff_ask(_record(writes=[], git=built)))
    assert "dispatch consume" in str(handoffs.handoff_ask(_record(row="review")))
    assert handoffs.handoff_ask(_record(kind="probe", git=built)) == "read the result; nothing to land"


def test_tasks_that_are_not_a_list_name_none_in_the_dispatch_log():
    d = _record(tasks="12", budget_default_usd=5.0, thread=[{"from": "manager", "text": "goal"}])
    assert "tasks none;" in handoffs.dispatch_log_text(d, None, "auto")


def test_convention_rows_of_the_wrong_type_are_skipped():
    lines = handoffs._conventions_lines(
        {"violations": ["junk"], "comments": ["junk", {"path": "a.py", "line": 1, "text": "x"}]}
    )
    assert "### Comments and docstrings added (1)" in lines
    assert "- `a.py:1` x" in lines


def test_a_notify_value_that_is_not_a_list_names_no_channel(tmp_root: Path):
    d = _record(notify="chat")
    dispatch.save(tmp_root, d)
    assert notify(tmp_root, d, "done") == {}


def test_a_waiting_dispatch_whose_result_is_not_an_object_shows_its_error(tmp_root: Path):
    dispatch.save(tmp_root, _record(state="failed", result="junk", error="boom", chat_pending=True))
    assert "boom" in pending_block(tmp_root)


def test_verification_and_ack_events_read_malformed_nested_values_as_missing(monkeypatch: pytest.MonkeyPatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(telemetry_events, "emit", lambda root, d, kind, **fields: seen.append(fields))
    d = _record(verification_telemetry={"attempt": 1, "event_id": "e"}, verification_result="junk")
    telemetry_events.verification(Path("."), d, None)
    telemetry_events.ack(Path("."), _record(actioned="note"))
    assert seen[0]["outcome"] == "incomplete"
    assert seen[1]["data"]["noted"] is False


def test_reverify_rows_that_are_not_objects_keep_the_lease(tmp_root: Path):
    (tmp_root / ".agent/dispatches").mkdir(parents=True)
    dispatch.save(tmp_root, _record(reverify_runs=["junk"]))
    lease = {
        "session_id": "reverify:999-01:r1",
        "area": "verify:web",
        "pid": 999999,
        "touched_at": "2000-01-01T00:00:00+00:00",
    }
    path = leases_mod.area_file(tmp_root, "verify:web")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lease))
    assert lane.reconcile_one(tmp_root, lease) is False
    assert path.exists()


def test_a_pr_number_that_is_not_an_int_leaves_the_record_unchanged(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    existing = {"number": 7, "url": "u", "state": "OPEN", "merged_at": None, "checked_at": None}
    d = _record(pr=existing)
    dispatch.save(tmp_root, d)
    items = [{"number": "42", "url": "u", "state": "OPEN", "mergedAt": None}]
    monkeypatch.setattr(
        dispatch.ledger.subprocess,
        "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(items), stderr=""),
    )
    assert dispatch.refresh_pr(tmp_root, d, GH_REPO, force=True) == existing
    assert d["pr"] == existing


def test_a_stored_return_that_is_not_an_object_reads_as_no_return():
    d = _record(result="junk", last_result={"type": "result", "return_in": "result"})
    assert dispatch.state.envelope_return(d, d["last_result"]) is None
    assert dispatch.state.pending_outcome_of(_record(pending_outcome="junk")) is None
