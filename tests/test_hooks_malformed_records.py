"""Hooks on malformed payloads and session records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import config_file, hooks, plan_review, sessions, tab_drain
from agent_cli.hooks import decision_log, guards, naming, plan_gate, task_notices
from agent_cli.hooks import notes_sync as notes_hook
from agent_cli.hooks.activity import desired_label

SID = "ae3b0000-0000-4000-8000-000000000001"
TAB = "mgr-008-x · cl ae3b"
NOTICE = "<task-notification><task-id>b1</task-id><status>completed</status></task-notification>"


def _labels(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sessions.set_record(tmp_root, SID, pid=4242, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, "activity", {"state": "idle"})
    labels: list[str] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: labels.append(label) or {"started": True},
    )
    return labels


@pytest.mark.parametrize("field", ["background_tasks", "subagent_permission"])
def test_a_list_where_the_session_record_keeps_a_map_counts_as_nothing_on_the_tab(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, field: str
):
    labels = _labels(tmp_root, monkeypatch)
    sessions.set_field(tmp_root, SID, field, ["b1"])
    hooks.activity(tmp_root, {"session_id": SID, "hook_event_name": "Notification"})
    assert labels[-1] == f"✅ {TAB}"


def test_a_user_entry_whose_message_is_not_an_object_delivers_nothing():
    assert task_notices.delivered_texts({"type": "user", "message": [NOTICE]}) == []
    assert task_notices.delivered_texts(
        {"type": "user", "message": {"content": [NOTICE, {"type": "text"}]}}
    ) == [""]


def test_a_scan_mark_that_is_not_an_object_rescans_from_the_start(tmp_root: Path, tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": NOTICE}}) + "\n", encoding="utf-8"
    )
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    record = {"transcript_scan": ["junk"]}
    payload = {"transcript_path": str(transcript)}
    assert task_notices.close_finished(tmp_root, SID, record, payload, {"b1": "t"}) == {}


@pytest.mark.parametrize(
    "scan",
    [
        {"unparsed": ["junk", {"from": 0, "to": 5, "ids": ["b1"]}]},  # rows that are not objects are skipped
        {
            "behind": "junk",
            "unparsed": [{"from": 0, "to": 5, "ids": ["b1"]}],
        },  # a junk behind count reads as 0
    ],
)
def test_stuck_line_reads_past_malformed_scan_values(scan: dict[str, object]):
    record = {"background_tasks": {"b1": "t"}, "transcript_scan": scan}
    assert "0-5 shows b1 finished, unconfirmed" in task_notices.stuck_line(record)


def test_a_tool_input_that_is_not_an_object_lets_no_chaining_allow(tmp_root: Path):
    assert guards.no_chaining(tmp_root, {"tool_input": "ls && rm -rf x"}) == ""


def test_plan_mode_input_that_is_not_an_object_presents_no_plan(tmp_root: Path):
    assert plan_gate.plan_gate(tmp_root, {"tool_name": "ExitPlanMode", "tool_input": "x"}) == ""


@pytest.mark.parametrize("body", ["junk", "[1, 2]", '"text"'])
@pytest.mark.parametrize("suffix", ["", "-1"])  # a legacy record and a round record
def test_a_review_record_that_is_not_an_object_counts_as_no_review(tmp_root: Path, body: str, suffix: str):
    text = "# plan\n\nstep one\n"
    reviews = plan_review.reviews_dir(tmp_root)
    reviews.mkdir(parents=True)
    (reviews / f"{plan_review.digest(text.encode())}{suffix}.json").write_text(body, encoding="utf-8")
    out = plan_gate.plan_gate(tmp_root, {"tool_name": "ExitPlanMode", "tool_input": {"plan": text}})
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "no blind review round of this plan yet" in reason


@pytest.mark.parametrize("shown", ["abc", {"a": 1}, 5])
def test_a_shown_list_that_is_not_a_list_hides_no_notice(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, shown: object
):
    monkeypatch.setattr(config_file, "notes_sync_mode", lambda root: "off")
    sessions.set_record(tmp_root, SID, name="mgr-008-x")
    sessions.set_field(tmp_root, SID, notes_hook.PENDING_KEY, {"id": "a", "status": "error", "detail": "d"})
    sessions.set_field(tmp_root, SID, notes_hook.SHOWN_KEY, shown)
    assert notes_hook.report_lines(tmp_root, SID) == ["notes sync error: d"]


@pytest.mark.parametrize(
    ("feature", "title"),
    [
        ("junk", "mgr-unmapped-ae3b00"),  # a feature that is not an object: unmapped
        (["id", 7], "mgr-unmapped-ae3b00"),
        (5, "mgr-unmapped-ae3b00"),
        ({"id": "junk", "slug": "x"}, "mgr-000-x"),  # an id that is not a number titles as zero
    ],
)
def test_a_malformed_feature_titles_the_session_unmapped_or_as_zero(feature: object, title: str):
    record = {"session_id": SID, "feature": feature}
    assert naming.desired_title(record) == title


def test_a_hook_specific_output_that_is_not_an_object_falls_back_to_the_reason():
    out = json.dumps({"permissionDecision": "deny", "hookSpecificOutput": "junk", "reason": "no"})
    assert decision_log._denied(out) == "no"


@pytest.mark.parametrize("permission", [["a1"], "a1"])
def test_a_subagent_permission_that_is_not_a_map_does_not_wait_on_the_owner(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, permission: object
):
    _labels(tmp_root, monkeypatch)
    record = {**(sessions.get(tmp_root, SID) or {}), "subagent_permission": permission}
    desired = desired_label(tmp_root, SID, record)
    assert desired is not None
    assert desired[2] == f"✅ {TAB}"


def test_a_finished_worker_is_the_managers_next_step_not_the_owners(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """A finished worker awaiting review read ✋, so the owner inspected routine
    completions. As ⏳ it read as still running. A later change keeps its count on the
    dashboard only; once actioned (landing marks it, as `ack` does) nothing waits.
    An owner question still raises the hand."""
    from agent_cli import dispatch as dispatch_mod
    from agent_cli.vault import live

    _labels(tmp_root, monkeypatch)
    finished = {
        "id": "001-mac-01",
        "launched_by_session": SID,
        "state": "done",
        "attempt": 1,
        "finished": "2026-10-11T10:00:00-06:00",
        "verdict_read": None,
    }
    monkeypatch.setattr(dispatch_mod, "load_all", lambda _root: [finished])
    record = sessions.get(tmp_root, SID) or {}
    owed = live.obligations(tmp_root, pairs=[(record, {})])
    assert [item["id"] for item in owed["manager_action"][SID]] == [finished["id"]]
    assert not owed["needs_you"]
    assert (desired_label(tmp_root, SID, record) or ("", "", ""))[2] == f"✅ {TAB}"
    finished["actioned_at"] = "2026-10-11T10:05:00-06:00"
    assert not live.obligations(tmp_root, pairs=[(record, {})])["manager_action"]
    assert (desired_label(tmp_root, SID, record) or ("", "", ""))[2] == f"✅ {TAB}"
    asked = {**record, "owner_question_pending": True}
    assert (desired_label(tmp_root, SID, asked) or ("", "", ""))[2] == f"✋ {TAB}"


def test_stuck_line_ignores_ids_that_are_a_string():
    record = {
        "background_tasks": {"b": "t"},
        "transcript_scan": {"unparsed": [{"from": 0, "to": 5, "ids": "b"}]},
    }
    assert task_notices.stuck_line(record) == ""


def test_a_pull_mark_with_a_junk_timestamp_retries_by_age():
    record = {notes_hook.PULL_KEY: {"pid": 7, "ok": False, "ts": "junk"}}
    assert notes_hook._needs_pull(record, 7, notes_hook.RETRY_S + 1)
    assert not notes_hook._needs_pull(record, 7, 1)
