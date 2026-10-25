"""The reduced worker return: the validator, both transport schemas and the handoff renderer.

Branch, commits, files_touched and commands left the return; git facts come from the record.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_cli.dispatch import handoffs
from agent_cli.dispatch.codex_normalize import codex_output_schema, structured_return
from agent_cli.dispatch.prompt import check_schema, verify_return
from agent_cli.dispatch.return_schema import RETURN_SCHEMA
from agent_cli.records.dispatch import git_of, result_of

REMOVED = ("branch", "commits", "files_touched", "commands")


def _ret(status: str = "done", **over: Any) -> dict[str, Any]:
    return {
        "nonce": "n-1",
        "status": status,
        "summary": "did it",
        "open_questions": [],
        "next_steps": [],
        "notes": "",
        **over,
    }


@pytest.mark.parametrize("status", ["done", "question", "blocked"])
def test_reduced_return_validates_for_every_status(status: str):
    assert verify_return({"nonce": "n-1"}, _ret(status)) == []
    assert verify_return({"nonce": "n-1"}, _ret(status, comments=[], conventions=[])) == []


@pytest.mark.parametrize("field", REMOVED)
def test_removed_fields_are_rejected_and_never_required(field: str):
    assert field not in RETURN_SCHEMA["required"] and field not in RETURN_SCHEMA["properties"]
    ret = {**_ret(), field: []}
    assert check_schema(ret, RETURN_SCHEMA) == [f"$.{field}: unexpected field"]


def test_malformed_and_mismatched_returns_still_fail():
    assert check_schema({"nonce": "n-1"}, RETURN_SCHEMA)
    assert check_schema(_ret(status="finished"), RETURN_SCHEMA)
    assert verify_return({"nonce": "other"}, _ret())[0].startswith("nonce mismatch")


def test_claude_and_codex_schemas_carry_the_reduced_contract():
    strict = codex_output_schema(RETURN_SCHEMA)
    assert set(strict["required"]) == set(RETURN_SCHEMA["properties"])
    assert not set(REMOVED) & set(strict["properties"])
    text = json.dumps({**_ret(), "comments": None, "conventions": None})
    parsed = structured_return(text)
    assert parsed == _ret()
    assert check_schema(parsed, RETURN_SCHEMA) == []
    assert structured_return("not json") is None


def test_findings_are_optional_and_typed():
    assert "findings" not in RETURN_SCHEMA["required"]
    row = {"id": "F1", "severity": "blocking", "summary": "s", "path": "a.py", "line": 3}
    assert verify_return({"nonce": "n-1"}, _ret(findings=[row])) == []
    assert verify_return({"nonce": "n-1"}, _ret(findings=[])) == []
    assert check_schema(_ret(findings=[{"severity": "urgent", "summary": "s"}]), RETURN_SCHEMA)
    assert check_schema(_ret(findings=[{"severity": "info"}]), RETURN_SCHEMA) == [
        "$.findings[0].summary: missing"
    ]
    strict = codex_output_schema(RETURN_SCHEMA)
    finding = strict["properties"]["findings"]["items"]
    assert set(finding["required"]) == {"id", "severity", "summary", "path", "line"}
    assert finding["properties"]["line"]["type"] == ["integer", "null"]
    assert finding["properties"]["severity"]["type"] == "string"
    text = json.dumps({**_ret(), "findings": [{**row, "path": None, "line": None}]})
    parsed = structured_return(text)
    assert parsed is not None and parsed["findings"] == [{"id": "F1", "severity": "blocking", "summary": "s"}]
    assert check_schema(parsed, RETURN_SCHEMA) == []


def test_verdict_validates_normalizes_and_preserves_the_whole_review_in_handoff():
    assert "verdict" not in RETURN_SCHEMA["required"]
    strict = codex_output_schema(RETURN_SCHEMA)
    field = strict["properties"]["verdict"]
    assert field["type"] == ["string", "null"] and field["enum"] == ["agree", "revise", None]
    nullable: dict[str, Any] = dict(comments=None, conventions=None, findings=None)
    assert check_schema(_ret(verdict=None, **nullable), strict) == []
    assert structured_return(json.dumps(_ret(verdict=None))) == _ret()
    summary = "Read every path.\n\nBlocking:\n- a.py:3 loses the input; probe reproduced it.\n\nChecked: all callers."
    for verdict in ("agree", "revise"):
        ret = _ret(summary=summary, verdict=verdict)
        assert verify_return({"nonce": "n-1"}, ret) == []
        assert check_schema({**ret, **nullable}, strict) == []
        assert structured_return(json.dumps(ret)) == ret
        assert verify_return({"nonce": "other"}, ret)[0].startswith("nonce mismatch")
        text = "\n".join(handoffs.return_lines(result_of({"result": ret}), None))
        assert " ".join(summary.split()) in text
    for invalid in (None, "Agree", "VERDICT: agree", "", ["agree"], {"verdict": "agree"}, 1):
        assert check_schema(_ret(verdict=invalid), RETURN_SCHEMA)
    assert "whole review" in RETURN_SCHEMA["properties"]["summary"]["description"]


def test_handoff_lists_git_facts_from_the_record_not_the_return():
    d = {
        "result": _ret(),
        "git": {"commits": [{"sha": "0123456789abcdef", "subject": "add it"}], "files": ["README.md"]},
    }
    text = "\n".join(handoffs.return_lines(result_of(d), git_of(d)))
    assert "- `0123456789` add it" in text and "- `README.md`" in text
    assert "commands" not in text
    none = "\n".join(handoffs.return_lines(result_of(d), None))
    assert "commits: none" in none and "files: none" in none


def test_handoff_still_reads_a_legacy_return():
    legacy = _ret(
        branch="b",
        commits=[{"sha": "abcdef0123456", "subject": "old"}],
        files_touched=["a.py"],
        commands=[{"cmd": "pytest", "exit": 0, "note": "ok"}],
    )
    text = "\n".join(handoffs.return_lines(result_of({"result": legacy}), git_of({"result": legacy})))
    assert "- `abcdef0123` old" in text and "- `a.py`" in text
    assert "- `pytest` → exit 0 — ok" in text
