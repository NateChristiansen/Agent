"""Every status change is logged with its reason, and a
blocked feature with work since the block is flagged in STATUS, the session block and doctor.
The status itself stays a choice: nothing here changes it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import blocked, features, log, status
from agent_cli.core import AgentError
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
    r = run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "waits on 041 landing",
            "--waits-on",
            "external",
            "--resume-when",
            "landed",
        ]
    )
    assert r.code == 0 and "status active → blocked, logged" in r.out
    last = entries(feature)[-1]
    assert (last.kind, last.text) == (
        "blocker",
        "status active → blocked: waits on 041 landing\nnext: external\nresume: landed",
    )
    run(["feature", "set", "1", "--status", "active", "--reason", "landed"])
    last = entries(feature)[-1]
    assert (last.kind, last.text) == ("note", "status blocked → active: landed")
    count = len(entries(feature))
    run(["feature", "set", "1", "--status", "active", "--title", "Renamed"])  # no change: no entry
    assert len(entries(feature)) == count


def test_a_blocked_feature_with_no_work_is_not_flagged(tmp_root: Path, run, feature: features.Feature):
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "Mac sign-in",
            "--waits-on",
            "owner",
            "--resume-when",
            "signed in",
        ]
    )
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
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "Mac sign-in",
            "--waits-on",
            "owner",
            "--resume-when",
            "signed in",
        ]
    )
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
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "x",
            "--waits-on",
            "manager",
            "--resume-when",
            "ready",
        ]
    )
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
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "x",
            "--waits-on",
            "manager",
            "--resume-when",
            "ready",
        ]
    )
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
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "waits on 041",
            "--waits-on",
            "external",
            "--resume-when",
            "landed",
        ]
    )
    text = session_hook._blocked_block(
        tmp_root, {"id": 1, "slug": "alpha-work", "dir": "features/001-alpha-work"}
    )
    assert "feature 001 is BLOCKED since " in text and "waits on 041" in text
    run(["feature", "set", "1", "--status", "active", "--reason", "landed"])
    assert session_hook._blocked_block(tmp_root, {"id": 1}) == ""


def test_status_json_carries_the_flag_for_the_dashboard(tmp_root: Path, run, feature: features.Feature):
    run(
        [
            "feature",
            "set",
            "1",
            "--status",
            "blocked",
            "--reason",
            "x",
            "--waits-on",
            "manager",
            "--resume-when",
            "ready",
        ]
    )
    _dispatch(tmp_root, "001-mac-01", state="running")
    payload = status.render(tmp_root)
    [f] = payload["features"]
    assert f["blocked_work"] == ["dispatch 001-mac-01 running"]
    assert f["blocked_since"]["reason"] == "x"


@pytest.mark.parametrize("initial", ["active", "blocked"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"reason": None},
        {"reason": " "},
        {"reason": "x" * 121},
        {"reason": "first\nsecond"},
        {"reason": "first\u2028second"},
        {"waits_on": None},
        {"waits_on": "nobody"},
        {"waits_on": "feature:999"},
        {"waits_on": "dispatch:"},
        {"waits_on": "dispatch:x\nnext: owner"},
        {"resume_when": None},
        {"resume_when": " "},
        {"resume_when": "first\rsecond"},
    ],
)
def test_invalid_block_refuses_before_any_write(tmp_root, feature, initial, invalid):
    valid = {"reason": "Waiting", "waits_on": "owner", "resume_when": "Approved"}
    if initial == "blocked":
        features.set_fields(
            tmp_root,
            feature,
            status="blocked",
            reason=valid["reason"],
            waits_on=valid["waits_on"],
            resume_when=valid["resume_when"],
        )
    before = {p.name: p.read_bytes() for p in feature.dir.iterdir() if p.is_file()}
    meta = dict(feature.meta)
    with pytest.raises(AgentError):
        features.set_fields(tmp_root, feature, status="blocked", title="Must not change", **(valid | invalid))
    assert feature.meta == meta
    assert {p.name: p.read_bytes() for p in feature.dir.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("terminal", ["done", "abandoned"])
def test_closed_dependency_refuses_before_writing(tmp_root, run, feature, terminal):
    run(["feature", "new", "dependency", "--title", "Dependency"])
    dependency = features.resolve(tmp_root, "2")
    dependency.body = "## Tasks\n"
    dependency.save()
    features.set_fields(tmp_root, dependency, status=terminal)
    before = feature.plan_path.read_bytes(), feature.log_path.read_bytes()
    with pytest.raises(AgentError, match="open feature"):
        features.set_fields(
            tmp_root, feature, status="blocked", reason="Waiting", waits_on="feature:002", resume_when="Ready"
        )
    assert (feature.plan_path.read_bytes(), feature.log_path.read_bytes()) == before


@pytest.mark.parametrize("actor", ["owner", "external", "manager", "dispatch:001-mac-01", "feature:002"])
def test_valid_blocker_actor(tmp_root, run, feature, actor):
    run(["feature", "new", "dependency", "--title", "Dependency"])
    features.set_fields(
        tmp_root, feature, status="blocked", reason="x" * 120, waits_on=actor, resume_when="Ready"
    )
    current = blocked.since(feature)
    assert current is not None and current.waits_on == actor and len(current.reason) == 120


@pytest.mark.parametrize("initial", ["proposed", "blocked"])
def test_activation_requires_reason_before_writing(tmp_root, feature, initial):
    feature.meta["status"] = initial
    feature.save()
    before = feature.plan_path.read_bytes(), feature.log_path.read_bytes()
    with pytest.raises(AgentError, match="needs a reason"):
        features.set_fields(tmp_root, feature, status="active", title="Must not change")
    assert (feature.plan_path.read_bytes(), feature.log_path.read_bytes()) == before
    features.set_fields(tmp_root, feature, status="active", reason="Owner approved")
    assert feature.status == "active"


def test_update_retains_start_and_work_but_refreshes_all_blocker_fields(tmp_root, feature, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(log, "now", lambda: datetime.fromisoformat("2026-10-20T10:00:00+00:00"))
    features.set_fields(
        tmp_root, feature, status="blocked", reason="First", waits_on="owner", resume_when="Approved"
    )
    first = blocked.since(feature)
    old_hash = status.source_hash(status.collect(tmp_root))
    monkeypatch.setattr(log, "now", lambda: datetime.fromisoformat("2026-10-22T10:00:00+00:00"))
    features.set_fields(
        tmp_root, feature, status="blocked", reason="Current", waits_on="external", resume_when="Shipped"
    )
    current = blocked.since(feature)
    assert first is not None and current is not None
    assert current.ts == first.ts and current.updated != first.updated
    assert current.reason == "Current" and current.waits_on == "external" and current.resume_when == "Shipped"
    assert [e.text for e in entries(feature) if e.kind == "blocker"] == [
        "status active → blocked: First\nnext: owner\nresume: Approved",
        "status blocked → blocked: Current\nnext: external\nresume: Shipped",
    ]
    work = blocked.active_work(
        tmp_root,
        feature,
        [{"id": "between", "feature": 1, "state": "done", "started": "2026-10-21T10:00:00+00:00"}],
    )
    assert work == ["dispatch between started 2026-10-21T10:00:00+00:00"]
    row = status.collect(tmp_root)["features"][0]
    assert row["blocker"] == current._asdict()
    assert status.source_hash(status.collect(tmp_root)) != old_hash
    assert any(
        "next: external · resume: Shipped" in line for line in blocked.session_lines(tmp_root, feature)
    )
    features.set_fields(tmp_root, feature, status="active", reason="Shipped")
    features.set_fields(
        tmp_root, feature, status="blocked", reason="New block", waits_on="manager", resume_when="Reviewed"
    )
    fresh = blocked.since(feature)
    assert fresh is not None and fresh.ts == current.updated


def test_legacy_block_reason_survives_until_complete_update(tmp_root, feature):
    feature.meta["status"] = "blocked"
    feature.save()
    log.append(feature, "blocker", "status active → blocked: Legacy reason")
    old = blocked.since(feature)
    assert old is not None and old.reason == "Legacy reason" and old.waits_on == "—"
    with pytest.raises(AgentError):
        features.set_fields(tmp_root, feature, status="blocked", reason="Replacement")
    assert blocked.since(feature) == old
    features.set_fields(
        tmp_root, feature, status="blocked", reason="Replacement", waits_on="manager", resume_when="Ready"
    )
    updated = blocked.since(feature)
    assert updated is not None and updated.ts == old.ts


def test_mcp_forwards_blocker_contract_and_refuses_incomplete_updates(tmp_root, feature):
    from agent_cli.mcp.specs_features import feature_set
    from agent_cli.mcp.tools import Ctx

    ctx = Ctx(root=tmp_root, profile="manager")
    with pytest.raises(AgentError):
        feature_set(ctx, "1", status="blocked", reason="Incomplete")
    feature_set(ctx, "1", status="blocked", reason="Waiting", waits_on="owner", resume_when="Approved")
    current = blocked.since(feature)
    assert current is not None and current.waits_on == "owner" and current.resume_when == "Approved"
