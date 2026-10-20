"""Every status change is logged with its reason, and a
blocked feature with work since the block is flagged in STATUS, the session block and doctor.
The status itself stays a choice: nothing here changes it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import blocked, features, log, status
from agent_cli.doctor import checks_state
from agent_cli.hooks import session as session_hook

pytestmark = pytest.mark.usefixtures("notes_remote")


def _dispatch(root: Path, ident: str, **fields: object) -> None:
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    record = {"id": ident, "feature": 1, "state": "done", **fields}
    (base / f"{ident}.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture
def feature(tmp_root: Path, run) -> features.Feature:
    run(["feature", "new", "alpha-work", "--title", "Alpha work"])
    return features.resolve(tmp_root, "1")


def entries(f: features.Feature) -> list[log.Entry]:
    return log.parse(f.log_path)


def test_every_status_change_is_logged_with_its_reason(tmp_root: Path, run, feature: features.Feature):
    r = run(["feature", "set", "1", "--status", "blocked", "--reason", "waits on 041 landing"])
    assert r.code == 0 and "status active → blocked, logged" in r.out
    last = entries(feature)[-1]
    assert (last.kind, last.text) == ("blocker", "status active → blocked: waits on 041 landing")
    run(["feature", "set", "1", "--status", "active"])
    last = entries(feature)[-1]
    assert (last.kind, last.text) == ("note", "status blocked → active: no reason given")
    count = len(entries(feature))
    run(["feature", "set", "1", "--status", "active", "--title", "Renamed"])  # no change: no entry
    assert len(entries(feature)) == count


def test_a_blocked_feature_with_no_work_is_not_flagged(tmp_root: Path, run, feature: features.Feature):
    run(["feature", "set", "1", "--status", "blocked", "--reason", "Mac sign-in"])
    f = features.resolve(tmp_root, "1")
    assert blocked.active_work(tmp_root, f, []) == []
    assert checks_state.blocked_with_work(tmp_root) == []
    run(["status", "render"])
    row = status.collect(tmp_root)["features"][0]
    assert row["status"] == "blocked" and row["blocked_since"]["reason"] == "Mac sign-in"
    assert row["blocked_work"] == []


def test_a_running_dispatch_flags_a_blocked_feature_everywhere(
    tmp_root: Path, run, feature: features.Feature
):
    run(["feature", "set", "1", "--status", "blocked", "--reason", "Mac sign-in"])
    _dispatch(tmp_root, "001-mac-01", state="running")
    run(["status", "render"])
    assert status.collect(tmp_root)["features"][0]["blocked_work"] == ["dispatch 001-mac-01 running"]
    [row] = checks_state.blocked_with_work(tmp_root)
    assert row["level"] == "warn" and row["check"] == "blocked 001" and "001-mac-01 running" in row["detail"]
    lines = blocked.session_lines(tmp_root, features.resolve(tmp_root, "1"))
    assert lines[0].startswith("feature 001 is BLOCKED since ") and lines[0].endswith(": Mac sign-in")
    assert "dispatch 001-mac-01 running" in lines[1]
    assert "feature_set status=active" in lines[2]


def test_dispatches_and_landings_count_only_after_the_block(tmp_root: Path, run, feature: features.Feature):
    run(["feature", "set", "1", "--status", "blocked", "--reason", "x"])
    f = features.resolve(tmp_root, "1")
    ts = blocked.since(f)
    assert ts is not None
    before, after = "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"
    old = {"id": "001-mac-02", "feature": 1, "state": "done", "started": before}
    new = {"id": "001-mac-03", "feature": 1, "state": "done", "started": after}
    landed = {
        "id": "001-mac-04",
        "feature": 1,
        "state": "done",
        "started": before,
        "actioned_at": after,
        "actioned": {"action": "landed"},
    }
    other = {"id": "002-mac-01", "feature": 2, "state": "running"}
    work = blocked.active_work(tmp_root, f, [old, new, landed, other])
    assert work == [f"dispatch 001-mac-03 started {after}", f"landed 001-mac-04 at {after}"]


def test_a_tick_on_a_blocked_feature_is_logged_and_flagged(tmp_root: Path, run, feature: features.Feature):
    run(["task", "add", "1", "first"])
    run(["feature", "set", "1", "--status", "blocked", "--reason", "x"])
    r = run(["task", "done", "1", "1"])
    assert "is blocked" in r.out and "feature_set status=active" in r.out
    assert entries(features.resolve(tmp_root, "1"))[-1].text == "task #1 ticked while the feature is blocked"
    work = blocked.active_work(tmp_root, features.resolve(tmp_root, "1"), [])
    assert len(work) == 1 and work[0].startswith("task #1 ticked ")


def test_a_block_set_before_the_log_existed_counts_only_running_work(tmp_root: Path, run):
    run(["feature", "new", "beta", "--title", "Beta", "--status", "blocked"])
    f = features.resolve(tmp_root, "1")
    assert blocked.since(f) is None
    done = {"id": "001-mac-01", "feature": 1, "state": "done", "started": "2999-01-01T00:00:00+00:00"}
    assert blocked.active_work(tmp_root, f, [done]) == []
    assert blocked.active_work(tmp_root, f, [{**done, "state": "queued"}]) == ["dispatch 001-mac-01 queued"]


def test_the_session_block_names_a_blocked_feature(tmp_root: Path, run, feature: features.Feature):
    run(["feature", "set", "1", "--status", "blocked", "--reason", "waits on 041"])
    text = session_hook._blocked_block(
        tmp_root, {"id": 1, "slug": "alpha-work", "dir": "features/001-alpha-work"}
    )
    assert "feature 001 is BLOCKED since " in text and "waits on 041" in text
    run(["feature", "set", "1", "--status", "active"])
    assert session_hook._blocked_block(tmp_root, {"id": 1}) == ""


def test_status_json_carries_the_flag_for_the_dashboard(tmp_root: Path, run, feature: features.Feature):
    run(["feature", "set", "1", "--status", "blocked", "--reason", "x"])
    _dispatch(tmp_root, "001-mac-01", state="running")
    payload = status.render(tmp_root)
    [f] = payload["features"]
    assert f["blocked_work"] == ["dispatch 001-mac-01 running"]
    assert f["blocked_since"]["reason"] == "x"
