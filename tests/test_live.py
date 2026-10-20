"""`.agent/live.json`, the snapshot the Dashboard's DataviewJS views render. Built
in Python from the same rules as `dispatch list`, written by hooks, the supervisor and render."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import core
from agent_cli import dispatch as dispatch_mod
from agent_cli.vault import live
from dispatch_helpers import _new, droot, envelope, one  # noqa: F401 — droot is a fixture

pytestmark = pytest.mark.usefixtures("notes_remote")


def _write_dispatch(root: Path, dispatch_id: str, **fields) -> None:
    record = {
        "id": dispatch_id,
        "feature": 1,
        "short": "alpha",
        "state": "running",
        "attempt": 1,
        "cost_usd": 0.0,
        "budget_usd": 2.0,
        "branch": "b",
        "worktree": "w",
        "handoff": None,
        "thread": [{"from": "manager", "text": "# Fix the header\nmore"}],
    }
    record.update(fields)
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _snapshot(root: Path) -> dict:
    return json.loads(live.path(root).read_text(encoding="utf-8"))


def test_workers_are_exactly_the_live_dispatches(tmp_root: Path, run, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    run(["feature", "new", "alpha", "--title", "A"])
    _write_dispatch(tmp_root, "001-01")
    _write_dispatch(
        tmp_root,
        "001-02",
        state="done",
        finished="2026-09-17T09:00:00-06:00",
        chat_pending=True,
        git={"commits": [{"sha": "a", "subject": "s"}], "files": ["x.py"]},
    )
    _write_dispatch(
        tmp_root,
        "001-03",
        state="done",
        finished="2026-09-17T09:00:00-06:00",
        actioned_at="2026-09-17T09:05:00-06:00",
    )
    _write_dispatch(tmp_root, "001-04", kind="probe")
    live.write(tmp_root)
    snap = _snapshot(tmp_root)
    listed = [d["id"] for d in dispatch_mod.list_(tmp_root)]
    assert [w["id"] for w in snap["workers"]] == listed == ["001-01", "001-02"]
    running = snap["workers"][0]
    assert running["goal"] == "Fix the header"
    assert running["feature_slug"] == "alpha" and running["ask"] is None
    assert running["open"].endswith("_dispatch=001-01")
    # A finished build nobody read or landed, with no live manager to owe it, is the owner's as
    # one orphaned row for its feature. Its ask is a build's: a branch with a diff.
    assert [(n["kind"], n.get("ids")) for n in snap["needs_you"]] == [("orphaned", ["001-02"])]
    assert snap["needs_you"][0]["action"] == "resume feature manager"
    assert snap["workers"][1]["ask"] == "review, then push or ack"


def test_a_running_worker_carries_last_activity_and_last_tool(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    _write_dispatch(tmp_root, "001-01", supervisor_pid=1)
    out = tmp_root / ".agent" / "dispatches" / "001-01.out"
    lines = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "model": "m", "text": "", "tools": ["Read", "Grep"]},
        {"type": "user", "tool_results": 2, "errors": []},
        {"type": "assistant", "model": "m", "text": "thinking", "tools": []},
    ]
    # padding ahead of the tail window, so the reader has to seek and skip a cut line
    out.write_text("x" * (live.TAIL_BYTES + 10) + "\n" + "\n".join(json.dumps(r) for r in lines) + "\n")
    worker = live.build(tmp_root)["workers"][0]
    assert worker["last_tool"] == "Grep"
    assert worker["last_activity"] and worker["supervisor_alive"] is True


def test_read_verdicts_leave_workers_but_unread_verdicts_and_orphans_stay(tmp_root: Path, run):
    from agent_cli.dispatch import verdicts

    run(["feature", "new", "alpha", "--title", "A"])
    for did in ("001-01", "001-02", "001-03"):
        _write_dispatch(tmp_root, did, state="done", chat_pending=True, verdict_read=None)
    d = dispatch_mod.load(tmp_root, "001-01")
    verdicts.mark(d, "responsible-manager", "consume")
    dispatch_mod.save(tmp_root, d)
    snapshot = live.build(tmp_root)
    assert [w["id"] for w in snapshot["workers"]] == ["001-02", "001-03"]
    assert len(snapshot["needs_you"]) == 1
    assert snapshot["needs_you"][0]["ids"] == ["001-02", "001-03"]


def test_last_tool_is_none_without_a_stream(tmp_path: Path):
    assert live.last_tool(tmp_path / "missing.out") is None
    (tmp_path / "empty.out").write_text("")
    assert live.last_tool(tmp_path / "empty.out") is None


def test_an_unchanged_snapshot_is_not_rewritten_until_a_heartbeat_is_due(tmp_root: Path, monkeypatch):
    monkeypatch.setenv("AGENT_NOW", "2026-09-17T09:00:00-06:00")
    assert live.write(tmp_root) is True
    assert live.write(tmp_root) is False
    assert live.write(tmp_root, heartbeat=True) is False
    monkeypatch.setenv("AGENT_NOW", "2026-09-17T09:00:06-06:00")
    assert live.write(tmp_root) is False
    assert live.write(tmp_root, heartbeat=True) is True
    assert _snapshot(tmp_root)["rendered_at"] == "2026-09-17T09:00:06-06:00"


def test_status_render_writes_the_snapshot(tmp_root: Path, run):
    run(["status", "render"])
    assert set(_snapshot(tmp_root)) == {
        "rendered_at",
        "observed_at",
        "valid_until",
        "managers",
        "workers",
        "leases",
        "needs_you",
        "manager_action",
        "tabs",
        "health",
    }


def test_leases_carry_their_status(tmp_root: Path, monkeypatch):
    from agent_cli import ops

    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    ops.lease_take(tmp_root, area="vault:src", session_id="s1")
    lease = live.build(tmp_root)["leases"]
    assert [(le["area"], le["holder"], le["status"]) for le in lease] == [("vault:src", "s1", "live")]


def test_a_snapshot_failure_never_raises(tmp_root: Path, monkeypatch, capsys):
    def boom(root):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(live, "build", boom)
    live.write_quietly(tmp_root)
    assert "live snapshot failed (disk gone)" in capsys.readouterr().err


def test_new_plans_and_handoffs_embed_their_views(droot: Path, fake_claude):
    plan = (droot / "features/001-alpha-work/plan.md").read_text(encoding="utf-8")
    assert plan.index('dv.view("harness/obsidian/views/feature")') < plan.index("## Context")
    fake_claude.scenario([one(envelope())])
    _new(droot)
    handoff = next((droot / "features/001-alpha-work/handoffs").glob("*.md")).read_text(encoding="utf-8")
    assert 'dv.view("harness/obsidian/views/worker", { id: "001-mac-01" })' in handoff
    assert handoff.index("## Live") < handoff.index("## Prompt")


def test_the_supervisor_writes_at_attempt_start_end_and_on_a_heartbeat(droot: Path, fake_claude, monkeypatch):
    beats: list[bool] = []
    monkeypatch.setattr(live, "HEARTBEAT_SECONDS", 0.0)
    monkeypatch.setattr(dispatch_mod.state, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(live, "write_quietly", lambda root, heartbeat=False: beats.append(heartbeat))
    fake_claude.scenario([one(envelope(), sleep=0.2)])
    _new(droot)
    assert True in beats  # the poller's heartbeat
    assert beats.count(False) >= 2  # attempt start and attempt end
