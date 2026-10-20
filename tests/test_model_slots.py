"""A finished worker gives its repo slot back before verification; finished work queues with a
bound (C7, F). Sonnet workers finished in 1 to 3 minutes and then held their slot for 13
to 43 minutes of waiting for the verify lane on 2026-10-06."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from agent_cli import leases
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import holdings, state
from dispatch_helpers import _new as new  # pyright: ignore[reportPrivateUsage] -- shared test helper
from dispatch_helpers import (
    droot,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
    envelope,
    one,
)


def test_a_dispatch_gives_its_slot_back_before_it_verifies(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope(), commit="a change, so the verifier runs")])
    d: dict[str, Any] = new(droot)
    assert d["slot_released"]["attempt"] == d["attempt"]
    assert "slot" not in d and "slot_schema" not in d
    assert d["verification_result"]["status"] == "verified"


def _record(root: Path, dispatch_id: str, **fields: Any) -> None:
    state.save(root, {"id": dispatch_id, "repo": "alpha-api", "short": "alpha", "attempt": 1, **fields})


def test_no_slot_is_given_back_while_worker_cleanup_is_unconfirmed(tmp_root: Path):
    _record(tmp_root, "001-mac-01", state="running", worker_cleanup_pending=True)
    assert holdings.release_slot(tmp_root, "001-mac-01", 1) is False
    assert "slot_released" not in state.load(tmp_root, "001-mac-01")


def test_a_released_slot_is_free_for_the_next_worker(tmp_root: Path):
    sid = "dispatch:001-mac-01"
    leases.take(tmp_root, "repo-slot:alpha/0", session_id=sid, pid=os.getpid())
    leases.take(tmp_root, "worktree:alpha/wt-1", session_id=sid, pid=os.getpid())
    _record(tmp_root, "001-mac-01", state="running", worker_cleanup_confirmed=True, slot=0, slot_schema=1)
    assert holdings.release_slot(tmp_root, "001-mac-01", 1) is True
    assert leases.get(tmp_root, "repo-slot:alpha/0") is None
    assert leases.get(tmp_root, "worktree:alpha/wt-1") is not None, "the artifact keeps its worktree"


def test_a_live_supervisor_s_slot_is_released_too(tmp_root: Path):
    """Codex's phase-3 review: with the supervisor alive, the lease layer kept the slot while
    the record said it was free."""
    sid = "dispatch:001-mac-01"
    leases.take(tmp_root, "repo-slot:alpha/0", session_id=sid, pid=os.getpid())
    leases.take(tmp_root, "worktree:alpha/wt-1", session_id=sid, pid=os.getpid())
    _record(
        tmp_root,
        "001-mac-01",
        state="running",
        supervisor_pid=os.getpid(),
        worker_cleanup_confirmed=True,
        slot=0,
        slot_schema=1,
    )
    assert holdings.release_slot(tmp_root, "001-mac-01", 1) is True
    assert leases.get(tmp_root, "repo-slot:alpha/0") is None
    assert state.load(tmp_root, "001-mac-01")["slot_released"]["areas"] == ["repo-slot:alpha/0"]
    assert leases.get(tmp_root, "worktree:alpha/wt-1") is not None, "a live supervisor keeps its worktree"


def _repo(**dispatch: object) -> repos_mod.Repo:
    return repos_mod.Repo(
        name="alpha-api", short="alpha", path="repos/alpha-api", remote="x", dispatch=dict(dispatch)
    )


def test_a_full_queue_of_finished_work_refuses_a_new_launch(tmp_root: Path):
    for i in (1, 2):
        _record(
            tmp_root,
            f"001-mac-0{i}",
            state="running",
            slot_released={"attempt": 1},
            supervisor_pid=os.getpid(),
        )
    with pytest.raises(AgentError, match="await verification"):
        holdings.refuse_backpressure(tmp_root, _repo(max_pending_verify=2))
    holdings.refuse_backpressure(tmp_root, _repo(max_pending_verify=3))


def test_a_pending_record_whose_supervisor_died_does_not_hold_back_work(tmp_root: Path):
    """Codex's phase-3 review: one stale record refused every launch after a restart."""
    _record(tmp_root, "001-mac-01", state="running", slot_released={"attempt": 1}, supervisor_pid=2**22 + 7)
    assert holdings.pending_artifacts(tmp_root, "alpha") == []
    holdings.refuse_backpressure(tmp_root, _repo(max_pending_verify=1))


def test_finished_and_older_attempts_do_not_count(tmp_root: Path):
    _record(tmp_root, "001-mac-01", state="done", slot_released={"attempt": 1})
    _record(tmp_root, "001-mac-02", state="running", attempt=2, slot_released={"attempt": 1})
    assert holdings.pending_artifacts(tmp_root, "alpha") == []
    assert holdings.pending_limit(_repo()) == 2
