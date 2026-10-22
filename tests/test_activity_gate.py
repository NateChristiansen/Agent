"""The activity gate core: implementation needs an active feature with nothing owed, and a
launch may not name a captured task awaiting approval. Every check re-reads the feature."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import activity_gate as gate
from agent_cli import features as features_mod
from agent_cli import reconcile
from agent_cli.core import AgentError
from agent_cli.dispatch import state

pytestmark = pytest.mark.usefixtures("notes_remote")


@pytest.fixture
def root(tmp_root: Path, run) -> Path:
    run(["feature", "new", "x", "--title", "X"])
    return tmp_root


def _status(root: Path, status: str) -> None:
    f = features_mod.resolve(root, "1")
    f.meta["status"] = status
    f.save()


@pytest.mark.parametrize(
    ("kind", "writes", "implementation"),
    [("work", None, True), ("work", ["src/**"], True), ("work", [], False), ("probe", None, False)],
)
def test_classification(kind: str, writes: list[str] | None, implementation: bool):
    assert gate.is_implementation(kind, writes) is implementation


@pytest.mark.parametrize("status", ["proposed", "blocked"])
def test_inactive_feature_refuses_implementation_but_not_reads(root: Path, status: str):
    stale = features_mod.resolve(root, "1")
    assert gate.launch_refusal(root, stale, kind="work", writes=None) is None
    _status(root, status)
    # the caller's Feature object is stale: the gate re-reads the status
    why = gate.launch_refusal(root, stale, kind="work", writes=None)
    assert why is not None and f"is {status}" in why
    assert gate.launch_refusal(root, stale, kind="work", writes=[]) is None
    assert gate.launch_refusal(root, stale, kind="probe", writes=None) is None
    with pytest.raises(AgentError, match=status):
        gate.require_launch(root, "1", kind="work", writes=["src/**"])


def test_owed_items_refuse_until_settled(root: Path):
    f = features_mod.resolve(root, "1")
    src = reconcile.add_manual(root, f, ["review finding"])["source"]
    why = gate.launch_refusal(root, "1", kind="work", writes=None)
    assert why is not None and "1 item(s) to reconcile" in why and src in why
    reconcile.decide(
        root,
        "1",
        src,
        [{"item": "review finding", "disposition": "waiting", "on": "owner", "until": "reply"}],
    )
    assert gate.launch_refusal(root, "1", kind="work", writes=None) is None


def test_held_captured_task_refuses_only_launches_that_name_it(root: Path, run):
    run(["task", "add", "1", "existing work"])
    f = features_mod.resolve(root, "1")
    src = reconcile.add_manual(root, f, ["finding"])["source"]
    cap = reconcile.capture(root, f, source=src, item="finding", text="fix the finding")
    assert gate.launch_refusal(root, "1", kind="work", writes=None, tasks=[1]) is None
    why = gate.launch_refusal(root, "1", kind="work", writes=None, tasks=[cap["task"]])
    assert why is not None and "awaits approval" in why
    reconcile.approve(root, "1", of=cap["capture"], evidence="owner approved in chat")
    assert gate.launch_refusal(root, "1", kind="work", writes=None, tasks=[cap["task"]]) is None


def test_land_refuses_unbound_inactive_and_owing_members(root: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli import worktree

    monkeypatch.setattr(worktree, "main_checkout", lambda _root: root)
    state.save(root, {"id": "001-mac-01", "repo": "harness", "short": "mac", "feature": 1, "attempt": 1})
    good = {"branch": "agent/a", "dispatch_id": "001-mac-01"}
    assert gate.land_refusals(root, [good]) == []
    unbound = gate.land_refusals(root, [good, {"branch": "agent/manual"}])
    assert len(unbound) == 1 and unbound[0].startswith("agent/manual: no dispatch record")
    assert "worktree_new" in unbound[0]
    _status(root, "blocked")
    assert gate.land_refusals(root, [good])[0].startswith("agent/a: Feature 001 is blocked")
    _status(root, "active")
    reconcile.add_manual(root, "1", ["acceptance"])
    assert "to reconcile" in gate.land_refusals(root, [good])[0]


def test_edit_gate_is_status_only_and_names_the_recovery(root: Path, run):
    refusal = gate.edit_refusal(root, session_id="s1")
    assert refusal is not None and "feature_use <n>" in refusal and "worktree_new" in refusal
    run(["feature", "use", "1", "--session-id", "s1"])
    assert gate.edit_refusal(root, session_id="s1") is None
    reconcile.add_manual(root, "1", ["owed but not an edit gate"])
    assert gate.edit_refusal(root, session_id="s1") is None
    _status(root, "proposed")
    refusal = gate.edit_refusal(root, session_id="s1")
    assert refusal is not None and "is proposed" in refusal and "feature_use 1" in refusal


def _bind(root: Path, monkeypatch: pytest.MonkeyPatch, branch: str, feature: int) -> None:
    from agent_cli import worktree

    monkeypatch.setattr(worktree, "main_checkout", lambda _root: root)
    path = root / ".agent" / worktree.BINDINGS
    table = json.loads(path.read_text()) if path.exists() else {}
    table[branch] = {"feature": feature, "session": None, "at": "2026-10-22T09:00:00+00:00"}
    path.write_text(json.dumps(table))


def test_a_worktree_binding_decides_landing_and_edits_before_the_session(
    root: Path, run, monkeypatch: pytest.MonkeyPatch
):
    run(["feature", "new", "y", "--title", "Y"])
    _bind(root, monkeypatch, "agent/one", 1)
    assert gate.land_refusals(root, [{"branch": "agent/one"}]) == []
    assert gate.edit_refusal(root, session_id=None, branch="agent/one") is None
    run(["feature", "use", "2", "--session-id", "s2"])
    _status(root, "blocked")
    # the binding to blocked 001 wins over the session mapped to active 002
    refusal = gate.edit_refusal(root, session_id="s2", branch="agent/one")
    assert refusal is not None and "is blocked" in refusal and "feature_use 1" in refusal
    assert gate.edit_refusal(root, session_id="s2", branch="agent/unbound") is None
    assert gate.land_refusals(root, [{"branch": "agent/one"}])[0].startswith(
        "agent/one: Feature 001 is blocked"
    )
    (root / ".agent" / "manager-worktrees.json").write_text("{broken")
    assert "not valid JSON" in gate.land_refusals(root, [{"branch": "agent/one"}])[0]
