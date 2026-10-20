from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from agent_cli import (
    capture,
    codex_tab_link,
    features,
    land_journal,
    lease_clear,
    leases,
    notes_sync,
    peers,
    session_liveness,
    sessions,
    status,
    tab_drain,
    tab_reset,
)
from agent_cli.core import AgentError

DAY = "2026-10-06"


def _session(root: Path, sid: str, **fields: Any) -> None:
    path = sessions.sessions_dir(root) / f"{sid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": sid, **fields}))


def _lease(root: Path, area: str, **fields: Any) -> Path:
    path = leases.area_file(root, area)
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = {
        "area": area,
        "session_id": "holder",
        "pid": os.getpid(),
        "touched_at": "2000-01-01T00:00:00+00:00",
    }
    path.write_text(json.dumps({**lease, **fields}))
    return path


def test_a_cost_by_day_amount_that_is_not_a_number_is_unknown(tmp_root: Path):
    _session(tmp_root, "s1", cost_by_day={DAY: "2"})
    assert sessions.cost_summary_for_day(tmp_root, DAY) == (0.0, False, True)
    assert sessions.cost_summary_for_day(tmp_root, DAY)[0] == 0.0


def test_a_cost_by_day_amount_that_is_not_a_number_restarts_the_day(tmp_root: Path):
    _session(tmp_root, "s1", cost_usd=0.0, cost_by_day={DAY: "1.5"})
    record = sessions.record_cost(tmp_root, "s1", 1.0, DAY)
    assert record is not None and record["cost_by_day"] == {DAY: 1.0}


@pytest.mark.parametrize("activity", ["working", [1], 5, True])
def test_an_unknown_pid_session_whose_activity_is_not_an_object_stays_live(activity: object):
    record = {"pid": None, "pid_unknown": True, "activity": activity}
    assert session_liveness.quiet(record) is False
    assert session_liveness.record_live(record) is True


def _queue_watch(root: Path, key: str = "watch-001-01") -> None:
    path = tab_drain.drain_dir(root) / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"key": key, "kind": "watch", "identity": "001-01", "match": "m", "label": "L"}
    path.write_text(json.dumps({**entry, "match_version": tab_drain.MATCH_VERSION, "generated": False}))


def test_painted_and_attempts_that_are_not_objects_are_reset_by_the_drainer(tmp_root: Path):
    _queue_watch(tmp_root)
    state: dict[str, Any] = {"painted": ["junk"], "attempts": "junk"}
    (entry,) = tab_drain._pending(tmp_root, state)
    assert entry["key"] == "watch-001-01"
    assert state["painted"] == {} and state["attempts"] == {}


def test_a_request_ignores_painted_and_attempts_that_are_not_objects(tmp_root: Path, monkeypatch):
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)

    class Process:
        pid = 99999

    monkeypatch.setattr(tab_drain.subprocess, "Popen", lambda *a, **kw: Process())
    tab_drain._save_state(tmp_root, {"painted": ["junk"], "attempts": ["junk"]})
    result = tab_drain.request(tmp_root, "watch", "001-01", "m", "L", generated=False)
    assert result == {"started": True, "queued": True}


def test_a_reset_for_a_session_whose_activity_is_not_an_object_is_dropped(tmp_root: Path):
    _session(tmp_root, "s1", activity="ended")
    key = tab_drain.drain_key("manager", "s1")
    path = tab_drain.drain_dir(tmp_root) / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"key": key, "kind": "manager", "identity": "s1", "match": "m", "label": "", "reset": True}
    path.write_text(json.dumps(entry))
    assert tab_drain._pending(tmp_root, {}) == []
    assert not path.exists()
    assert tab_reset.wanted(tmp_root, entry) is False


def test_a_painted_reset_whose_session_activity_is_not_an_object_is_restored(tmp_root: Path, monkeypatch):
    _session(tmp_root, "s1", activity="ended")
    restored: list[str] = []
    monkeypatch.setattr(tab_reset, "_restore", lambda root, sid, marker, *, cleared: restored.append(sid))
    attempt = tab_reset.Attempt(key="manager-s1", session_id="s1", marker="m", outcome="done")
    assert tab_reset._reconcile(tmp_root, attempt) == set()
    assert restored == ["s1"]


@pytest.mark.usefixtures("notes_remote")
def test_a_lease_request_list_with_junk_rows_keeps_the_rest(tmp_root: Path):
    path = _lease(tmp_root, "vault:src", requests=["junk", {"session_id": "s2", "pid": 1}])
    assert leases.request(tmp_root, "vault:src", session_id="s3", pid=None) is True
    rows = json.loads(path.read_text())["requests"]
    assert [r["session_id"] for r in rows] == ["s2", "s3"]


@pytest.mark.parametrize(
    ("feature", "named"),
    [
        ("033", "(no feature,"),  # a feature that is not an object
        ({"id": "abc"}, "(no feature,"),  # ids that are not a number
        ({"id": "3x"}, "(no feature,"),
        ({"id": True}, "(no feature,"),
        ({"id": 3.7}, "(no feature,"),
        ({"id": []}, "(no feature,"),
        ({"id": "33"}, "("),  # a digit string reads as its number
        ({"id": "033"}, "("),
        ({"id": 33}, "("),
    ],
)
def test_a_refusal_names_the_feature_number_or_no_feature(tmp_root: Path, feature: object, named: str):
    text = leases.refusal(tmp_root, "vault:src", {"session_id": "h", "feature": feature})
    assert named in text


@pytest.mark.usefixtures("notes_remote")
def test_a_live_lease_with_malformed_feature_and_requests_still_blocks(tmp_root: Path):
    path = _lease(tmp_root, "vault:src", touched_at=None, feature="033", requests="junk")
    with pytest.raises(AgentError, match="held by holder"):
        leases.take(tmp_root, "vault:src/agent_cli/dispatch", session_id="s2", pid=os.getpid())
    assert path.exists()


def test_a_lease_file_that_is_not_an_object_is_malformed(tmp_root: Path):
    path = leases.area_file(tmp_root, "vault:src")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["vault:src", "holder"]))
    with pytest.raises(AgentError, match="malformed"):
        leases.get(tmp_root, "vault:src")


def test_lease_clear_logs_no_feature_for_a_malformed_one(tmp_root: Path, monkeypatch):
    resolved: list[str] = []

    def resolve(root: Path, ref: str):
        resolved.append(ref)
        raise AgentError("no such feature")

    monkeypatch.setattr(features, "resolve", resolve)
    held = [
        {"area": "vault:src", "feature": "033"},
        {"area": "vault:a", "feature": {"id": "abc"}},
        {"area": "vault:b", "feature": {"id": "33"}},
        {"area": "vault:c", "feature": {"id": "033"}},
    ]
    lease_clear._log(tmp_root, held, "s1", ["vault:src", "vault:a", "vault:b", "vault:c"])
    assert resolved == ["33"]


def test_a_land_journal_that_is_not_an_object_is_refused(tmp_path: Path):
    path = land_journal.path(tmp_path, "j1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["done"]))
    with pytest.raises(AgentError, match="not a land journal"):
        land_journal.read(tmp_path, "j1")
    assert land_journal.every(tmp_path) == []


def test_a_reservation_watermark_that_is_not_an_int_reads_as_zero(tmp_root: Path):
    _session(tmp_root, "s1", **{peers.WAKE_FIELD: {"id": "w1", "watermark": "9"}})
    peers.acknowledge_observed(tmp_root, "s1", {"id": "w1", "watermark": 3})
    assert (sessions.get(tmp_root, "s1") or {})[peers.WAKE_FIELD] is None


def test_a_stale_marker_watermark_that_is_not_an_int_reads_as_zero(tmp_root: Path):
    path = peers.stale_path(tmp_root, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "w1", "watermark": "5"}))
    assert peers.marked_stale(tmp_root, "s1", {"id": "w1", "watermark": 3}) is False


def test_a_notes_commit_message_reads_a_malformed_feature_as_unmapped(tmp_path):
    # The session's feature is in the body since the subject names staged paths.
    def body(session: dict[str, object]) -> str:
        return notes_sync._message(tmp_path, session, ["journal/x.md"])[1]

    assert "(unmapped)" in body({"feature": "033"})
    assert "(unmapped)" in body({"feature": {"id": "abc", "slug": "x"}})
    assert "(033-x)" in body({"feature": {"id": "33", "slug": "x"}})


def test_a_rollout_payload_that_is_not_an_object_has_no_originator(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    created = time.time()
    day = tmp_path / "sessions" / time.strftime("%Y/%m/%d", time.localtime(created))
    day.mkdir(parents=True)
    (day / "rollout-x-s1.jsonl").write_text(json.dumps({"payload": "codex_cli"}) + "\n")
    assert codex_tab_link.thread_originator("s1", created) == ""


@pytest.mark.usefixtures("notes_remote")
def test_a_subagent_transcript_path_that_is_not_a_string_is_skipped(tmp_root: Path, run):
    run(["feature", "new", "alpha", "--title", "Alpha"])
    feature = features.resolve(tmp_root, "1")
    feature.raw_dir.mkdir(parents=True, exist_ok=True)
    (feature.raw_dir / "2026-10-06.jsonl").write_text(
        json.dumps({"extra": {"agent_transcript_path": 5}}) + "\n"
    )
    assert capture.preserve_transcripts(tmp_root, feature) == []


@pytest.mark.usefixtures("notes_remote")
def test_feature_repos_that_are_not_a_list_name_no_repo_shorts(tmp_root: Path, run):
    run(["feature", "new", "alpha", "--title", "Alpha"])
    feature = features.resolve(tmp_root, "1")
    feature.meta["repos"] = "web"
    feature.save()
    (row,) = status.collect(tmp_root)["features"]
    assert row["repo_shorts"] == []
