"""The manager's plan-review findings ledger, its verifier and the disputes the owner sees.

The verifier launch is mocked: `dispatch.new` records its arguments and starts nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, hooks, ops, plan_review, sessions
from agent_cli import plan_review_ledger as ledger
from agent_cli.core import AgentError

SID = "4b1c2d3e-0000-4000-8000-00000000c1a1"
PLAN = "# Plan\n\n## Context\n\nThe owner wants X.\n\n## Steps\n\n1. Build X.\n"


def _plan(root: Path) -> str:
    path = root / "features" / "001-x" / "plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLAN, encoding="utf-8")
    return str(path)


def _round(root: Path, plan: str, round_no: int, client: str, texts: list[str]) -> list[str]:
    sha = plan_review.digest(PLAN.encode())
    added = ledger.add_findings(
        root,
        plan,
        plan_sha=sha,
        round_no=round_no,
        dispatch_id=f"001-mac-{round_no:02d}-{client}",
        client=client,
        texts=texts,
    )
    return [str(e["id"]) for e in added]


def test_findings_and_a_disposition_are_written_and_read_back(tmp_root: Path):
    plan = _plan(tmp_root)
    ids = _round(tmp_root, plan, 1, "codex", ["Step 1 has no test.", "The gate reads one line."])
    assert ids == ["F1", "F2"]
    ledger.dispose(tmp_root, plan, finding_id="F1", disposition="fixed", note="## Steps")
    ledger.dispose(tmp_root, plan, finding_id="F2", disposition="rejected", note="out of scope")

    path = tmp_root / ".agent" / "plan-reviews" / "ledgers" / f"{ledger.plan_key(tmp_root, plan)}.json"
    assert path == ledger.ledger_path(tmp_root, plan) and path.is_file()
    resolved = str(plan_review.resolve_plan(tmp_root, plan)).encode()
    assert ledger.plan_key(tmp_root, plan) == hashlib.sha256(resolved).hexdigest()
    data = ledger.load(tmp_root, plan)
    first, second = data["findings"]
    assert first["reviewer"] == {"dispatch_id": "001-mac-01-codex", "client": "codex"}
    assert first["round"] == 1 and first["plan_sha"] == plan_review.digest(PLAN.encode())
    assert first["text"] == "Step 1 has no test." and first["verifier"] is None
    assert first["disposition"]["state"] == "fixed" and first["disposition"]["section"] == "## Steps"
    assert second["disposition"]["state"] == "rejected" and second["disposition"]["reason"] == "out of scope"
    # Not read as a review record by the gate's reader.
    assert plan_review.all_records(tmp_root) == []


def test_the_typed_action_reads_and_disposes(tmp_root: Path):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "claude", ["A finding."])
    op = ops.dispatch_plan_ledger(tmp_root, paths=[plan], finding_id="F1", disposition="rejected", note="no")
    assert op.data["disposition"]["reason"] == "no"
    read = ops.dispatch_plan_ledger(tmp_root, paths=[plan])
    assert [f["id"] for f in read.data["findings"]] == ["F1"] and read.data["disputes"] == []
    with pytest.raises(AgentError, match="needs the reason"):
        ops.dispatch_plan_ledger(tmp_root, paths=[plan], finding_id="F1", disposition="rejected")
    with pytest.raises(AgentError, match="no finding"):
        ops.dispatch_plan_ledger(tmp_root, paths=[plan], finding_id="F9", disposition="fixed", note="x")


@pytest.mark.parametrize("body", ["{not json", "[]", '{"findings": {}}', '{"findings": [{"text": "x"}]}'])
def test_a_malformed_ledger_raises_rather_than_reading_empty(tmp_root: Path, body: str):
    plan = _plan(tmp_root)
    path = ledger.ledger_path(tmp_root, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    with pytest.raises(AgentError, match="ledger"):
        ledger.load(tmp_root, plan)
    with pytest.raises(AgentError):
        ops.dispatch_plan_ledger(tmp_root, paths=[plan])
    with pytest.raises(AgentError):
        _round(tmp_root, plan, 1, "codex", ["lost?"])
    assert path.read_text(encoding="utf-8") == body


_GOOD = {
    "id": "F1",
    "plan_sha": "a" * 64,
    "round": 1,
    "reviewer": {"dispatch_id": "d", "client": "codex"},
    "text": "t",
    "verifier": None,
    "disposition": None,
    "reraise_of": None,
}


@pytest.mark.parametrize(
    "override",
    [
        {"round": "broken"},
        {"round": True},
        {"plan_sha": None},
        {"text": 3},
        {"reviewer": "codex"},
        {"reviewer": {"client": "codex"}},
        {"verifier": {"result": "maybe", "label": None, "reason": "r", "dispatch_id": "v"}},
        {"verifier": {"result": "kept", "label": "huge", "reason": "r", "dispatch_id": "v"}},
        {"disposition": {"state": "later"}},
        {"disposition": "fixed"},
        {"reraise_of": 4},
    ],
)
def test_malformed_entries_raise_on_read(tmp_root: Path, override: dict[str, Any]):
    plan = _plan(tmp_root)
    path = ledger.ledger_path(tmp_root, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"findings": [{**_GOOD, **override}], "verifiers": {}}), encoding="utf-8")
    with pytest.raises(AgentError, match="malformed"):
        ledger.load(tmp_root, plan)


def test_a_bare_id_finding_and_a_bad_verifier_entry_raise(tmp_root: Path):
    plan = _plan(tmp_root)
    path = ledger.ledger_path(tmp_root, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    for body in (
        {"findings": [{"id": "F1"}]},
        {"findings": [_GOOD], "verifiers": {"1": "d"}},
        {"findings": [_GOOD], "verifiers": {"1": {"plan_sha": "x"}}},
    ):
        path.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(AgentError, match="malformed"):
            ledger.load(tmp_root, plan)


def test_a_well_formed_entry_loads(tmp_root: Path):
    plan = _plan(tmp_root)
    path = ledger.ledger_path(tmp_root, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    verifier = {"result": "refuted", "label": None, "reason": "r", "dispatch_id": "v"}
    entry = {**_GOOD, "verifier": verifier, "disposition": {"state": "fixed", "section": "s"}}
    path.write_text(
        json.dumps({"findings": [entry], "verifiers": {"1": {"dispatch_id": "v"}}}), encoding="utf-8"
    )
    assert ledger.load(tmp_root, plan)["findings"][0]["verifier"] == verifier


def test_a_ledger_with_invalid_utf8_raises_agent_error(tmp_root: Path):
    plan = _plan(tmp_root)
    path = ledger.ledger_path(tmp_root, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"findings": [], "x": "\xff"}')
    with pytest.raises(AgentError, match="ledger"):
        ledger.load(tmp_root, plan)


def test_plan_mode_does_not_allow_plan_ledger(tmp_root: Path):
    def allowed(action: str) -> bool:
        payload = {"tool_name": "mcp__agent__dispatch", "tool_input": {"action": action}}
        out, code = hooks.run(tmp_root, "plan-mode-allow", json.dumps(payload))
        assert code == 0
        return bool(out) and "allow" in out

    assert allowed("plan-review")
    assert not allowed("plan-ledger")


# --- the verifier ---------------------------------------------------------------------------------


def test_the_verifier_launch_is_pinned_and_blind(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "claude", ["Claude says the gate is weak."])
    _round(tmp_root, plan, 1, "codex", ["Codex says   step 1\nhas no test."])
    sha = plan_review.digest(PLAN.encode())
    snapshot = plan_review.reviews_dir(tmp_root) / f"{sha}.md"
    snapshot.write_text(PLAN, encoding="utf-8")
    calls: list[dict[str, Any]] = []

    def fake(root: Path, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return {"id": "001-mac-09", "state": "queued"}

    monkeypatch.setattr(dispatch, "new", fake)
    monkeypatch.setattr(
        "agent_cli.verify_record.harness_repo",
        lambda root: type("Repo", (), {"name": "agent", "default_branch": "main"}),
    )
    sessions.set_record(tmp_root, SID, name="mgr-001-x")

    assert ledger.launch_verifier(tmp_root, plan_path=plan, round_no=1, session_id=SID)["id"] == "001-mac-09"
    [kw] = calls
    assert (kw["provider"], kw["model"], kw["effort"], kw["row"]) == (
        "codex",
        "gpt-6.1-sol",
        "high",
        "review",
    )
    assert kw["writes"] == [] and kw["no_checks"] is True
    order = kw["prompt"]
    assert "- F1: Claude says the gate is weak." in order and "- F2: Codex says step 1 has no test." in order
    assert ledger.VERIFIER_INSTRUCTION in order and "when unsure, keep it" in order
    assert f"BEGIN PLAN SNAPSHOT sha256 {sha}" in order and "The owner wants X." in order
    for identity in ("001-mac-01-claude", "001-mac-01-codex", "client", "reviewer"):
        assert identity not in order
    assert ledger.load(tmp_root, plan)["verifiers"]["1"] == {"dispatch_id": "001-mac-09", "plan_sha": sha}


def test_a_second_launch_for_a_round_launches_nothing(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "codex", ["a"])
    sha = plan_review.digest(PLAN.encode())
    (plan_review.reviews_dir(tmp_root) / f"{sha}.md").write_text(PLAN, encoding="utf-8")
    calls: list[dict[str, Any]] = []

    def fake(root: Path, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return {"id": f"001-mac-{len(calls):02d}", "state": "queued"}

    monkeypatch.setattr(dispatch, "new", fake)
    monkeypatch.setattr(
        "agent_cli.verify_record.harness_repo",
        lambda root: type("Repo", (), {"name": "agent", "default_branch": "main"}),
    )
    sessions.set_record(tmp_root, SID, name="mgr-001-x")
    ledger.launch_verifier(tmp_root, plan_path=plan, round_no=1, session_id=SID)
    with pytest.raises(AgentError, match="001-mac-01"):
        ledger.launch_verifier(tmp_root, plan_path=plan, round_no=1, session_id=SID)
    assert len(calls) == 1
    assert ledger.load(tmp_root, plan)["verifiers"]["1"]["dispatch_id"] == "001-mac-01"


def test_the_verifier_needs_the_frozen_snapshot(tmp_root: Path):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "codex", ["x"])
    with pytest.raises(AgentError, match="snapshot"):
        ledger.launch_verifier(tmp_root, plan_path=plan, round_no=1, session_id=SID)


def test_the_table_is_parsed_and_missing_or_bad_rows_are_named():
    summary = (
        "Verifier table\n\n"
        "| id | result | label | reason |\n"
        "|---|---|---|---|\n"
        "| F1 | kept | blocking | read plan_gate.py:61, the gate checks one line |\n"
        "| F2 | refuted | - | probe: `pytest tests/x.py` passes |\n"
        "F3 | kept | optional | a bar not in the acceptance |\n"
        "| F4 | maybe | blocking | unsure |\n"
        "| F5 | kept | | no label |\n"
    )
    parsed, missing = ledger.parse_table(summary, ["F1", "F2", "F3", "F4", "F5", "F6"])
    assert parsed["F1"] == {
        "result": "kept",
        "label": "blocking",
        "reason": "read plan_gate.py:61, the gate checks one line",
    }
    assert parsed["F2"]["result"] == "refuted" and parsed["F2"]["label"] is None
    assert parsed["F3"]["label"] == "optional"
    assert missing == ["F4", "F5", "F6"]


def test_a_finding_listed_twice_gets_no_result():
    parsed, missing = ledger.parse_table("| F1 | kept | blocking | a |\n| F1 | refuted | - | b |", ["F1"])
    assert parsed == {} and missing == ["F1"]


def test_the_verifier_result_lands_in_the_ledger(tmp_root: Path):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "codex", ["a", "b"])
    missing = ledger.record_verifier(
        tmp_root, plan, round_no=1, summary="| F1 | kept | blocking | read it |", dispatch_id="001-mac-09"
    )
    assert missing == ["F2"]
    first, second = ledger.load(tmp_root, plan)["findings"]
    assert first["verifier"] == {
        "result": "kept",
        "label": "blocking",
        "reason": "read it",
        "dispatch_id": "001-mac-09",
    }
    assert second["verifier"] is None


# --- disputes -------------------------------------------------------------------------------------


def test_a_missing_or_bad_row_clears_the_stored_result(tmp_root: Path):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "claude", ["a", "b"])
    table = "| F1 | refuted | - | probe | \n| F2 | kept | blocking | read |"
    assert ledger.record_verifier(tmp_root, plan, round_no=1, summary=table, dispatch_id="v") == []
    assert ledger.load(tmp_root, plan)["findings"][0]["verifier"]["result"] == "refuted"
    bad = "| F1 | maybe | blocking | unsure |\n| F2 | kept | blocking | read |"
    assert ledger.record_verifier(tmp_root, plan, round_no=1, summary=bad, dispatch_id="v2") == ["F1"]
    first, second = ledger.load(tmp_root, plan)["findings"]
    assert first["verifier"] is None and second["verifier"]["dispatch_id"] == "v2"
    assert ledger.disputes(ledger.load(tmp_root, plan)) == []


def test_disputes_name_a_refuted_and_an_optional_claude_finding(tmp_root: Path):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "claude", ["refuted one", "optional one", "blocking one"])
    _round(tmp_root, plan, 1, "codex", ["codex refuted"])
    table = (
        "| F1 | refuted | - | probe shows it works |\n"
        "| F2 | kept | optional | not in acceptance |\n"
        "| F3 | kept | blocking | real |\n"
        "| F4 | refuted | - | read shows it works |\n"
    )
    assert ledger.record_verifier(tmp_root, plan, round_no=1, summary=table, dispatch_id="v") == []
    found = ledger.disputes(ledger.load(tmp_root, plan))
    assert [(d["kind"], d["finding"]["id"]) for d in found] == [
        ("claude-refuted", "F1"),
        ("claude-optional", "F2"),
    ]


def test_a_reraised_rejected_finding_is_a_dispute(tmp_root: Path):
    plan = _plan(tmp_root)
    _round(tmp_root, plan, 1, "codex", ["The gate reads one line.", "Same words again."])
    ledger.dispose(tmp_root, plan, finding_id="F1", disposition="rejected", note="by design")
    ledger.dispose(tmp_root, plan, finding_id="F2", disposition="rejected", note="by design")
    _round(tmp_root, plan, 2, "codex", ["Only the first summary line is read.", "same words  AGAIN."])
    # Reworded, so only the same-text re-raise counts until the manager links it.
    before = ledger.disputes(ledger.load(tmp_root, plan))
    assert [(d["kind"], d["finding"]["id"]) for d in before] == [("reraised-rejected", "F4")]

    op = ops.dispatch_plan_ledger(tmp_root, paths=[plan], finding_id="F3", reraise_of="F1")
    assert op.data["reraise_of"] == "F1"
    found = ops.dispatch_plan_ledger(tmp_root, paths=[plan]).data["disputes"]
    assert [(d["kind"], d["finding"]["id"], d["rejected"]["id"]) for d in found] == [
        ("reraised-rejected", "F3", "F1"),
        ("reraised-rejected", "F4", "F2"),
    ]
    with pytest.raises(AgentError, match="later round"):
        ledger.link_reraise(tmp_root, plan, finding_id="F1", earlier_id="F2")
