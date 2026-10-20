"""The record keeps one copy of the latest return, through cleanup-pending recovery and for
attempts and records written under the earlier contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch
from agent_cli.dispatch import state, terminal
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- droot is a pytest fixture

LEGACY = {
    "branch": "feature/alpha-work",
    "commits": [{"sha": "deadbeef", "subject": "old"}],
    "files_touched": ["README.md"],
    "commands": [{"cmd": "uv run pytest", "exit": "DENIED"}],
}


def _rewrite(root: Path, dispatch_id: str, **fields: Any) -> None:
    path = dispatch.file(root, dispatch_id, ".json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(fields)
    path.write_text(json.dumps(raw), encoding="utf-8")


def _question(root: Path, fake_claude: Any) -> dict[str, Any]:
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    return _new_fast(root)


def test_cleanup_pending_keeps_one_return_and_collect_restores_it(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(terminal, "requires_lease", lambda *_args: True)
    d = _question(droot, fake_claude)
    assert d["verification_cleanup_pending"] and not d["terminal_committed"]
    assert d["state"] == "question" and d["result"]["open_questions"] == ["which port?"]
    assert "result" not in d["pending_outcome"]
    assert d["last_result"]["return_in"] == "result" and "structured_output" not in d["last_result"]
    monkeypatch.undo()
    [summary] = dispatch.collect(droot, dispatch_id=d["id"])
    after = dispatch.load(droot, d["id"])
    assert summary["state"] == after["state"] == "question" and after["terminal_committed"]
    assert after["result"]["open_questions"] == ["which port?"]
    assert "pending_outcome" not in after


def test_a_legacy_pending_outcome_keeps_its_own_return(droot: Path, fake_claude, monkeypatch):
    monkeypatch.setattr(terminal, "requires_lease", lambda *_args: True)
    d = _question(droot, fake_claude)
    legacy = {**d["result"], **LEGACY, "summary": "legacy pending"}
    _rewrite(droot, d["id"], pending_outcome={**d["pending_outcome"], "result": legacy}, result=None)
    monkeypatch.undo()
    dispatch.collect(droot, dispatch_id=d["id"])
    after = dispatch.load(droot, d["id"])
    assert after["state"] == "question" and after["result"]["summary"] == "legacy pending"
    # Old records read as they always did, the worker's DENIED command row included.
    assert any("uv run pytest" in line for line in dispatch.permission_denials(after))


def test_an_incomplete_attempt_with_a_legacy_return_still_collects(droot: Path, fake_claude):
    d = _question(droot, fake_claude)
    old = {**envelope("question", open_questions=["which port?"]), "session_id": d["session_id"]}
    old["structured_output"] = {**old["structured_output"], **LEGACY, "nonce": d["nonce"]}
    _rewrite(
        droot,
        d["id"],
        state="running",
        terminal_committed=False,
        supervisor_pid=None,
        last_result=old,
        result=None,
        git=None,
    )
    dispatch.collect(droot, dispatch_id=d["id"])
    after = dispatch.load(droot, d["id"])
    assert after["state"] == "question", after.get("error")
    assert not set(LEGACY) & set(after["result"])
    assert after["last_result"]["return_in"] == "result"
    assert any("uv run pytest" in line for line in dispatch.permission_denials(after))


def test_a_missing_or_malformed_return_still_fails(droot: Path, fake_claude):
    broken = envelope()
    broken["structured_output"] = {"nonce": "{{NONCE}}", "status": "done"}
    fake_claude.scenario([one(broken)])
    d = _new_fast(droot)
    assert d["state"] == "failed" and "missing" in d["error"]
    # A return that failed validation is not moved into `result`; nothing claims it was accepted.
    assert d["result"] is None and "return_in" not in d["last_result"]


def test_a_stored_envelope_without_its_return_cannot_be_accepted_again():
    d = {"result": None, "last_result": {"type": "result", "return_in": "result"}}
    assert state.envelope_return(d, d["last_result"]) is None
    assert state.stored_envelope({"result": "{}", "structured_output": {}}) == {"structured_output": {}}
    kept = {"result": "not json", "structured_output": None}
    assert state.stored_envelope(kept) == kept
