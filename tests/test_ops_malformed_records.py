from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, ops, sessions
from agent_cli import peers as peers_mod
from agent_cli.cli import feature as cli_feature
from agent_cli.core import AgentError, Output
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.ops import infra, peer_remote, peer_wake, peer_wake_policy
from agent_cli.ops import peers as ops_peers
from agent_cli.ops.dispatch_preview import verification_preview_lines
from dispatch_helpers import malformed_record as _record
from harness_root import link_notes
from test_setup import no_shelling, quiet_doctor, tools  # noqa: F401 -- pytest fixtures

SID = "ae3b0000-0000-4000-8000-000000000001"


def test_dispatch_show_reads_a_result_that_is_not_an_object_as_missing(tmp_root: Path):
    (tmp_root / ".agent/dispatches").mkdir(parents=True)
    dispatch.save(tmp_root, _record(result="junk"))
    text = ops.dispatch_show(tmp_root, dispatch_id="999-01").text
    assert "worker-reported" not in text


def test_planned_checks_that_are_not_objects_are_not_listed():
    verification = {
        "status": "unverified",
        "configuration": "repo",
        "launch_allowed": True,
        "reason": "r",
        "commands": ["junk"],
        "timeout_seconds": 5,
    }
    lines = verification_preview_lines(verification)
    assert not any("planned supervisor checks" in line for line in lines)


def test_reverify_reasons_that_are_not_a_list_print_no_reasons(monkeypatch: pytest.MonkeyPatch):
    row = {
        "result": {"status": "failed", "reasons": "ab"},
        "worker_tree": {"observed": False},
        "final_sha": "0" * 40,
    }
    monkeypatch.setattr(reverify_mod, "reverify", lambda root, dispatch_id: row)
    text = ops.dispatch_reverify(Path("."), dispatch_id="999-01").text
    assert "reason:" not in text


@pytest.mark.usefixtures("notes_remote")
def test_a_cleanup_that_returns_no_list_reports_no_worktrees(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "x", "--title", "X"])
    monkeypatch.setattr(
        dispatch, "cleanup", lambda root, *, feature, force, session_id: {"removed": "ab", "skipped": "c"}
    )
    op = ops.feature_close(tmp_root, feature="1")
    assert op.data["worktrees_removed"] == [] and op.data["worktrees_kept"] == []
    assert "worktree" not in op.text


def test_feature_close_prints_no_worktree_notes_for_a_value_that_is_not_a_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    data = {"worktrees_removed": "ab", "worktrees_kept": "c"}
    monkeypatch.setattr(cli_feature.ops, "feature_close", lambda root, **kw: ops.Op(data, "closed"))
    args = argparse.Namespace(feature="1", status="done", note=None, session_id=None)
    assert cli_feature.feature_close(Path("."), args, Output()) == 0
    assert "worktree" not in capsys.readouterr().err


def test_setup_skips_obsidian_plugin_rows_that_are_not_objects(
    tmp_root: Path,
    tools,  # noqa: F811
    no_shelling,  # noqa: F811
    quiet_doctor,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    link_notes(tmp_root)
    monkeypatch.setattr(
        infra, "obsidian_setup", lambda root: ops.Op({"written": "ab", "plugins": ["junk"]}, "")
    )
    op = ops.setup(tmp_root)
    step = next(s for s in op.data["configure"] if s["step"] == "obsidian setup")
    assert step["state"] != "failed"
    assert step["detail"] == "0 file(s) in .obsidian/"


def test_a_plan_review_session_provider_that_is_not_a_string_still_launches_both_reviewers(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, "provider", ["codex"])
    plan = tmp_root / ".agent" / "scratch" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# plan\n", encoding="utf-8")
    launches: list[dict[str, Any]] = []

    def fake_new(root: Path, **kw: Any) -> dict[str, Any]:
        launches.append(kw)
        return {"id": f"001-mac-0{len(launches)}", "state": "queued"}

    monkeypatch.setattr(
        "agent_cli.verify_record.harness_repo",
        lambda root: argparse.Namespace(name="agent", default_branch="main"),
    )
    monkeypatch.setattr(dispatch, "new", fake_new)
    ops.dispatch_plan_review(tmp_root, paths=[str(plan)], session_id=SID)
    assert [(k["provider"], k["model"], k["effort"]) for k in launches] == [
        ("codex", "gpt-6.1-sol", "high"),
        ("claude", "sonnet", "high"),
    ]
    with pytest.raises(AgentError, match="exactly one plan path"):
        ops.dispatch_plan_review(tmp_root, paths=[str(plan), str(plan)], session_id=SID)


def test_earlier_messages_that_are_not_a_list_are_not_listed():
    result = {"outcome": "unconfirmed", "id": "m1", "earlier": "ab", "backlog": 0}
    assert "Earlier" not in peer_remote.summary(result, "mac")


def test_a_wake_reservation_that_is_not_an_object_is_replaced(tmp_root: Path):
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, peers_mod.WAKE_FIELD, "junk")
    wake_id = peer_wake.reserve(tmp_root, SID)
    assert wake_id is not None
    assert (sessions.get(tmp_root, SID) or {})[peers_mod.WAKE_FIELD]["id"] == wake_id


def test_settling_over_a_reservation_that_is_not_an_object_leaves_it(tmp_root: Path):
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, peers_mod.WAKE_FIELD, "junk")
    peer_wake.settle(tmp_root, SID, "w1", True)
    assert (sessions.get(tmp_root, SID) or {})[peers_mod.WAKE_FIELD] == "junk"


def test_a_deferral_list_that_is_not_a_list_restarts(tmp_root: Path):
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, peer_wake_policy.DEFER_FIELD, "ab")
    peer_wake_policy.defer(tmp_root, SID, "sender")
    rows = (sessions.get(tmp_root, SID) or {})[peer_wake_policy.DEFER_FIELD]
    assert [r["from"] for r in rows] == ["sender"]


def test_an_activity_that_is_not_an_object_reads_as_not_ended(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(ops_peers, "is_alive", lambda pid: True)
    sessions.set_record(tmp_root, SID, name="mgr-008-x", pid=1)
    sessions.set_field(tmp_root, SID, "provider", "codex")
    sessions.set_field(tmp_root, SID, "pid_unknown", True)
    sessions.set_field(tmp_root, SID, "activity", "junk")
    assert [r["session_id"] for r in ops_peers.addressable_peers(tmp_root)] == [SID]
    assert [r["session_id"] for r in ops_peers.quiet_codex(tmp_root)] == [SID]
