"""Dispatch runtime readers (holdings, reverify, notices, test selection, Codex, setup) on
malformed record values."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli import procs, sessions
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import (
    codex_normalize,
    holdings,
    lane,
    mcp_policy,
    notice_delivery,
    notice_producers,
    notices,
    options,
    preflight,
    reverify,
    rubric,
    state,
    verification_evidence,
    verification_gate,
    worktrees,
)
from agent_cli.dispatch import test_select as select_mod
from agent_cli.dispatch.mcp_policy import resolve
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- droot is a pytest fixture

SID = "11111111-2222-3333-4444-555555555555"
HARNESS = "h" * 32


def _repo(**dispatch: Any) -> repos_mod.Repo:
    return repos_mod.Repo("Product", "product", "/nonexistent/clone", "unused", dispatch=dispatch)


@pytest.mark.parametrize(
    "rows",
    [["junk"], "junk", [{"run_id": "other", "binding": "junk"}], [{"run_id": "r1", "binding": "junk"}]],
)
def test_a_reverify_row_it_cannot_read_still_blocks_the_lane(tmp_path: Path, monkeypatch, rows):
    monkeypatch.setattr(state, "load", lambda root, did: {"reverify_runs": rows})
    lease = {"session_id": "reverify:999-01:r1", "checkout": str(tmp_path / "gone")}
    assert holdings.run_blocks(tmp_path, lease) is True


def test_binding_of_refuses_a_value_that_is_not_an_object():
    with pytest.raises(TypeError):
        verification_evidence.binding_of("junk")


def test_a_lane_with_no_rows_and_no_checkout_is_free(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(state, "load", lambda root, did: {"reverify_runs": []})
    lease = {"session_id": "reverify:999-01:r1", "checkout": str(tmp_path / "gone")}
    assert holdings.run_blocks(tmp_path, lease) is False


def test_held_holdings_that_are_not_an_object_still_refuse_abandon(tmp_path: Path):
    with pytest.raises(AgentError, match="keeps its worktree lease"):
        holdings.abandon(tmp_path, {"id": "999-01", "holdings_held": "junk"})


@pytest.mark.parametrize("rows", [["junk", {"run_id": "r1"}], {"a": 1}, 5])
def test_abort_checkout_keeps_the_record_and_lease_when_rows_are_malformed(tmp_path: Path, monkeypatch, rows):
    d: dict[str, Any] = {"id": "999-01", "short": "p", "reverify_runs": rows}
    released: list[str] = []
    monkeypatch.setattr(verification_gate, "record_guard", lambda root, did: nullcontext(d))
    monkeypatch.setattr(reverify, "save", lambda root, rec: None)
    monkeypatch.setattr(holdings, "reverify_release", lambda root, rec, run_id: released.append(run_id))
    with pytest.raises(ValueError, match="invalid reverify_runs"):
        reverify._abort_checkout(tmp_path, d, tmp_path, tmp_path / "h", tmp_path / "gone", "why", "r1")
    assert d["reverify_runs"] == rows and released == []


def test_reverify_runs_that_are_not_a_list_have_no_cleanup_pending():
    assert reverify.cleanup_pending({"reverify_runs": 5}) is False


def test_a_scoped_label_counts_no_tests_when_they_are_a_string():
    d = {
        "attempt": 1,
        "verification_scopes": [{"attempt": 1, "scope": "scoped", "tests": "abc", "paths": ["x"]}],
    }
    assert (
        select_mod.label(d)
        == "scoped check: 0 test files for 1 changed paths; the full suite runs at landing"
    )


def test_rates_skip_unmapped_paths_that_are_not_strings():
    records = [{"verification_scopes": [{"scope": "full", "reason": "unknown-path", "paths": [1, "a"]}]}]
    assert select_mod.rates(records).endswith("; unmapped paths: a")


def _entry(**extra: Any) -> dict[str, Any]:
    return {"generation": 1, "delivery": "pending", "consumption": "none", **extra}


def _outbox(entry: dict[str, Any]) -> dict[str, Any]:
    return {"id": "999-01", notices.OUTBOX_FIELD: {"n1": entry}, notices.GENERATION_FIELD: 1}


def _claim(**route: Any) -> notice_delivery.Claim:
    return notice_delivery.Claim(
        "999-01", "n1", "tok", 1, 0.0, 1e18, {"harness": HARNESS, "recipient": SID}, route
    )


def test_a_claim_that_is_not_an_object_has_expired_on_recheck(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(notice_delivery, "load", lambda root, did: _outbox(_entry(claim="junk")))
    assert notice_delivery.recheck(tmp_path, _claim(), clock=lambda: 0.0) == "claim-expired"


def test_a_claim_that_is_not_an_object_does_not_settle(tmp_path: Path, monkeypatch):
    d = _outbox(_entry(claim="junk"))
    monkeypatch.setattr(notice_delivery, "bounded_record_guard", lambda root, did, timeout: nullcontext(d))
    outcome = notice_delivery.settle(tmp_path, _claim(), "rung", None, clock=lambda: 0.0)
    assert outcome.settled is False and outcome.failure == "claim-expired"


def test_route_target_reads_session_activity_that_is_not_an_object_as_live(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sessions, "get", lambda root, sid: {"activity": "junk", "provider": "other"})
    route = {
        "version": notices.ROUTE_VERSION,
        "locality": notices.ROUTE_LOCALITY,
        "harness": HARNESS,
        "launched_by_session": SID,
    }
    assert notice_delivery.route_target(tmp_path, _claim(**route)) == (None, "route-unsupported")


def test_launch_route_reads_session_activity_that_is_not_an_object_as_live(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sessions, "get", lambda root, sid: {"activity": "junk"})
    monkeypatch.setattr(notice_producers, "new_route", lambda root, sid: {"launched_by_session": sid})
    assert notice_producers.launch_route(tmp_path, {"launched_by_session": SID}) == {
        "launched_by_session": SID
    }


def test_a_recipient_that_is_not_a_string_is_none():
    assert notice_producers.recipient({"envelope": {"recipient": 5}}) is None
    assert notice_producers.recipient({"route": {"launched_by_session": 5}}) is None


@pytest.mark.parametrize(
    "row",
    [
        {"binding": "junk", "result": {"status": "incomplete"}},
        {"binding": {"run_id": "r1"}, "result": "junk"},
    ],
)
def test_reverify_raised_skips_a_row_it_cannot_read(tmp_path: Path, monkeypatch, row):
    d: dict[str, Any] = {"id": "999-01", "attempt": 1, "state": "done", "reverify_runs": [row]}
    monkeypatch.setattr(verification_gate, "record_guard", lambda root, did: nullcontext(d))
    notice_producers.reverify_raised(tmp_path, "999-01", "r1", None)
    assert "chat_pending" not in d


def test_mcp_names_refuse_a_tuple():
    with pytest.raises(AgentError, match="dispatch.mcp must contain"):
        mcp_policy._names(("a",), "dispatch.mcp")


def test_a_required_list_that_is_a_string_requires_nothing():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"}
    assert codex_normalize.codex_output_schema(schema)["properties"]["a"]["type"] == ["string", "null"]


def test_tasks_that_are_a_string_name_no_tasks(tmp_path: Path):
    repo = repos_mod.Repo("Product", "product", str(tmp_path), "unused")
    result = resolve(tmp_path, repo, {"id": "001-01", "feature": 1, "tasks": "23"})
    assert result.servers["agent"]["env"]["AGENT_DISPATCH_TASKS"] == ""


def test_a_dispatch_env_that_is_not_a_mapping_adds_nothing(tmp_path: Path):
    repo = _repo(env=["A=1"])
    assert "A=1" not in options.worker_env(tmp_path, {"id": "999-01", "feature": 999}, repo)
    assert "A=1" not in options.verifier_env(repo)


def test_setup_that_is_a_string_runs_nothing(tmp_path: Path, monkeypatch):
    ran: list[str] = []

    def run_bounded(cmd: str, **kwargs: Any) -> SimpleNamespace:
        ran.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(procs, "run_bounded", run_bounded)
    assert worktrees.run_worktree_setup(tmp_path, _repo(setup="ab"), tmp_path) == []
    assert ran == []


def test_cleanup_keeps_a_worktree_whose_collected_files_are_not_an_object(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    (Path(d["worktree"]) / "output.txt").write_text("first\n", encoding="utf-8")
    d = state.load(droot, d["id"])
    d["collected_files"] = ["output.txt"]
    state.save(droot, d)
    skipped = worktrees.cleanup(droot, feature="1", force=True)["skipped"]
    assert "uncollected files: output.txt" in skipped[0]["reason"]
    assert "`dispatch` action `collect-files`" in skipped[0]["reason"]


def test_setup_that_is_a_string_is_not_preflighted(tmp_path: Path):
    d = {"id": "999-01", "thread": [{"from": "manager", "text": "do it"}]}
    rows = preflight(tmp_path, d, _repo(setup="ab"), {"permissions": {"allow": [], "deny": []}})
    assert [r for r in rows if r.get("source") == "setup"] == []


@pytest.mark.parametrize("value", ["abc", [1], 2.5, {"n": 2}, True, None])
def test_an_unreadable_concurrency_limit_reads_as_one(value: object):
    assert holdings.cap_of(_repo(max_concurrent=value)) == 1
    assert lane.lanes_of(_repo(max_concurrent_verify=value)) == 1


def test_an_absent_worker_limit_reads_as_three():
    assert holdings.cap_of(_repo()) == 3
    assert lane.lanes_of(_repo()) == 1


def test_a_concurrency_limit_that_is_an_int_is_kept():
    assert holdings.cap_of(_repo(max_concurrent=3)) == 3
    assert lane.lanes_of(_repo(max_concurrent_verify=2)) == 2


@pytest.mark.parametrize("value", [True, "30", [30], 10**400])
def test_an_unreadable_verify_wait_refuses_instead_of_taking_the_default(value: object):
    with pytest.raises(AgentError, match="verify_wait_timeout_s"):
        lane.wait_limit(_repo(verify_wait_timeout_s=value))


def test_a_verify_wait_that_is_a_number_is_kept():
    assert lane.wait_limit(_repo(verify_wait_timeout_s=30)) == 30.0


@pytest.mark.parametrize("value", [None, 0])
def test_a_verify_wait_that_is_unset_takes_the_default(value: object):
    assert lane.wait_limit(_repo(verify_wait_timeout_s=value)) == lane.WAIT_TIMEOUT_S


@pytest.mark.parametrize("value", ["abc", [1], True, 10**400])
def test_an_unreadable_repo_budget_refuses_instead_of_taking_the_default(value: object):
    with pytest.raises(AgentError, match="max_budget_usd"):
        rubric.repo_budget(_repo(max_budget_usd=value), 2.0)


def test_a_repo_budget_that_is_a_number_is_kept_and_none_takes_the_default():
    assert rubric.repo_budget(_repo(max_budget_usd=5), 2.0) == 5.0
    assert rubric.repo_budget(_repo(max_budget_usd=None), 2.0) == 2.0


@pytest.mark.parametrize("value", [5, ["m"]])
def test_a_model_pin_that_is_not_text_is_ignored(value: object):
    assert rubric.resolve(_repo(model=value), "claude", None, None, None, None) == (
        None,
        None,
        None,
        "default",
    )


def test_escalate_ignores_a_repo_model_and_effort_that_are_not_text():
    old = {"provider": "claude"}
    assert rubric.escalate(old, _repo(model=["m"], effort=1), None, None) == (None, None)


def test_resolve_ignores_a_repo_effort_that_is_not_text():
    repo = _repo(effort=[1])
    assert rubric.resolve(repo, "claude", None, "m", None, None) == (
        "m",
        None,
        None,
        "default",
    )
