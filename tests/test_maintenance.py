"""The maintenance gate: a parked feature, or an open window, refuses every dispatch start before
it has any effect, and only the owner's agent.local.yaml clears it."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import dispatch, leases, maintenance, ops, status
from agent_cli.core import AgentError
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401


def _local(root: Path, body: str) -> None:
    (root / "agent.local.yaml").write_text(body, encoding="utf-8")


def _records(root: Path) -> list[str]:
    path = root / ".agent" / "dispatches"
    return sorted(p.name for p in path.glob("*.json")) if path.exists() else []


def test_a_parked_feature_refuses_launch_before_any_effect(droot: Path, fake_claude):
    _local(droot, "maintenance:\n  parked: ['001']\n  reason: WIP archived\n")
    with pytest.raises(AgentError, match=r"feature 001 is parked \(WIP archived\); dispatch launch refused"):
        _new_fast(droot)
    assert _records(droot) == [] and fake_claude.calls() == []
    assert leases.get(droot, "repo-slot:alpha/0") is None


def test_a_dry_run_preview_shows_the_refusal_without_side_effects(droot: Path, fake_claude):
    _local(droot, "maintenance:\n  parked: [1]\n")
    with pytest.raises(AgentError, match="parked"):
        _new_fast(droot, dry_run=True)
    assert _records(droot) == []


def test_a_window_refuses_every_start(droot: Path, fake_claude):
    _local(droot, "maintenance:\n  window: audit repair in progress\n")
    with pytest.raises(AgentError, match=r"maintenance window \(audit repair in progress\)"):
        _new_fast(droot)
    assert _records(droot) == []


def test_resume_and_escalate_of_a_parked_feature_are_refused(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    d = _new_fast(droot)
    assert d["state"] == "question"
    _local(droot, "maintenance:\n  parked: ['001']\n")
    with pytest.raises(AgentError, match="dispatch resume refused"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="8080")
    with pytest.raises(AgentError, match="dispatch escalate refused"):
        _new_fast(droot, escalate_from=d["id"])
    after = dispatch.load(droot, d["id"])
    assert after["attempt"] == 1 and after["state"] == "question"
    assert len(fake_claude.calls()) == 1


def test_read_only_and_closing_operations_still_work_while_parked(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    d = _new_fast(droot)
    _local(droot, "maintenance:\n  parked: ['001']\n")
    assert [r["id"] for r in dispatch.list_(droot, all_=True)] == [d["id"]]
    dispatch.abandon(droot, dispatch_id=d["id"], note="parked for the repair")
    assert dispatch.load(droot, d["id"])["state"] == "abandoned"


def test_resume_into_a_closed_feature_is_refused(droot: Path, fake_claude, run):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    d = _new_fast(droot)
    # A close with open tasks is refused, so tick them first and check the close took.
    for task in ops.task_list(droot, feature="1", open_only=True).data["tasks"]:
        ops.task_done(droot, feature="1", task=str(task["index"]))
    assert run(["feature", "set", "1", "--status", "done"]).code == 0
    with pytest.raises(AgentError, match="feature 001 is done"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="8080")


@pytest.mark.parametrize(
    "body",
    [
        "maintenance: yes\n",
        "maintenance:\n  parked: notalist\n",
        "maintenance:\n  parked: ['x1']\n",
        "maintenance:\n  window: ''\n",
        "maintenance:\n  extra: 1\n",
    ],
)
def test_a_malformed_block_refuses_rather_than_stops_gating(tmp_path: Path, body: str):
    _local(tmp_path, body)
    with pytest.raises(AgentError, match="maintenance"):
        maintenance.check(tmp_path, 1, "launch")
    assert maintenance.parked(tmp_path) == []


def test_no_block_is_no_gate(tmp_path: Path):
    maintenance.check(tmp_path, 1, "launch")
    _local(tmp_path, "notes_sync: 'off'\n")
    maintenance.check(tmp_path, 1, "resume")


def test_status_marks_a_parked_feature_and_hides_its_next_task(droot: Path):
    _local(droot, "maintenance:\n  parked: ['001']\n")
    status.render(droot)
    row = status.collect(droot)["features"][0]
    assert row["parked"] is True
    assert row["status"] == "active"


def test_parking_in_the_main_checkout_holds_in_its_linked_worktrees(tmp_path: Path):
    """Codex review: the gate is machine-wide, so a linked worktree reads the main checkout's file."""
    import subprocess

    main = tmp_path / "main"
    main.mkdir()
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "base")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=main, check=True)
    _local(main, "maintenance:\n  parked: [1]\n")
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "side", str(wt)], cwd=main, check=True, capture_output=True
    )
    for checkout in (main, wt):
        with pytest.raises(AgentError, match="parked"):
            maintenance.check(checkout, 1, "launch")
