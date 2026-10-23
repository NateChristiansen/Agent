"""resume, cleanup and the no-checks verification gate run their Git work with
no records guard held (K0 addendum A3), and resume releases `dispatch-alloc` before it recreates a
worktree. Each test holds the operation at a Git step and writes a record in the same repo."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, leases, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import lifecycle, worktrees
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.allocation import GUARD_TIMEOUT_S
from agent_cli.dispatch.state import load, save
from dispatch_helpers import _new_fast, _set_alpha_dispatch, droot, envelope, one, settle  # noqa: F401
from test_dispatch_alloc import _no_detach


class Hold:
    """Stops the wrapped call where `when` matches until `release`, after saying it got there."""

    def __init__(self) -> None:
        self.entered, self.release = threading.Event(), threading.Event()

    def wrap(self, real: Callable[..., Any], when: Callable[..., bool] = lambda *a, **k: True):
        def held(*args: Any, **kwargs: Any) -> Any:
            if when(*args, **kwargs):
                self.entered.set()
                assert self.release.wait(10)
            return real(*args, **kwargs)

        return held

    def during(self, operation: Callable[[], Any], body: Callable[[], None]) -> Any:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(operation)
            try:
                assert self.entered.wait(10)
                body()
            finally:
                self.release.set()
            return future.result(timeout=30)


def _write(root: Path, dispatch_id: str, **fields: Any) -> None:
    started = time.monotonic()
    with gate.record_guard(root, dispatch_id) as current:
        current.update(fields)
        save(root, current)
    assert time.monotonic() - started < GUARD_TIMEOUT_S


def test_resume_recreates_its_worktree_with_no_guard_held(droot: Path, fake_claude, monkeypatch):
    _set_alpha_dispatch(droot, max_concurrent=2)
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    first = _new_fast(droot)
    main = repos.load_registry(droot).require("alpha").abs_path(droot)
    subprocess.run(["git", "worktree", "remove", "--force", first["worktree"]], cwd=main, check=True)
    _no_detach(monkeypatch)
    hold = Hold()
    monkeypatch.setattr(
        lifecycle,
        "ensure_worktree",
        hold.wrap(lifecycle.ensure_worktree, lambda root, rp, wt, *a, **kw: str(wt) == first["worktree"]),
    )
    second: dict[str, Any] = {}

    def beside() -> None:
        second.update(_new_fast(droot, watch=False))
        _write(droot, second["id"], written_during_resume=True)

    resumed = hold.during(
        lambda: dispatch.resume(droot, dispatch_id=first["id"], answer="use port 8080", watch=False), beside
    )
    assert resumed["attempt"] == 2 and Path(first["worktree"]).is_dir()
    assert (load(droot, first["id"])["slot"], second["slot"]) == (0, 1)
    assert load(droot, second["id"])["written_during_resume"]


def test_resume_refuses_a_record_that_changed_during_its_git_work(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    first = _new_fast(droot)
    main = repos.load_registry(droot).require("alpha").abs_path(droot)
    subprocess.run(["git", "worktree", "remove", "--force", first["worktree"]], cwd=main, check=True)
    _no_detach(monkeypatch)
    hold = Hold()
    monkeypatch.setattr(lifecycle, "ensure_worktree", hold.wrap(lifecycle.ensure_worktree))

    def resume() -> None:
        with pytest.raises(AgentError, match="stale dispatch record"):
            dispatch.resume(droot, dispatch_id=first["id"], answer="use port 8080", watch=False)

    hold.during(resume, lambda: _write(droot, first["id"], written_during_resume=True))
    after = load(droot, first["id"])
    assert after["state"] == "question" and after["attempt"] == 1 and after["written_during_resume"]
    assert not [x for x in leases.list_all(droot) if x.get("session_id") == f"dispatch:{first['id']}"]


def _done(root: Path) -> dict:
    d = _new_fast(root)
    assert d["state"] == "done"
    return d


@pytest.mark.parametrize("step", [("ls-files",), ("status",), ("worktree", "remove")])
def test_cleanup_runs_git_with_no_records_guard_held(droot: Path, fake_claude, monkeypatch, step):
    fake_claude.scenario([one(envelope())])
    d = _done(droot)
    settle(droot, d)
    hold = Hold()

    def at_step(args: list[str], *rest: Any, **kwargs: Any) -> bool:
        return tuple(args[: len(step)]) == step

    monkeypatch.setattr(worktrees.repos_mod, "run_git", hold.wrap(worktrees.repos_mod.run_git, at_step))
    monkeypatch.setattr(
        worktrees.repos_mod, "git_or_none", hold.wrap(worktrees.repos_mod.git_or_none, at_step)
    )
    refusals: list[str] = []

    def beside() -> None:
        _write(droot, d["id"], written_during_cleanup=True)
        try:
            dispatch.resume(droot, dispatch_id=d["id"], answer="one more thing", watch=False)
        except AgentError as exc:
            refusals.append(str(exc))

    out = hold.during(lambda: dispatch.cleanup(droot, feature="1"), beside)
    after = load(droot, d["id"])
    assert after["written_during_cleanup"] and after["state"] == "done" and after["attempt"] == 1
    assert len(refusals) == 1 and "reserved by cleanup:1:" in refusals[0]
    if step == ("worktree", "remove"):
        assert out["removed"] == [d["worktree"]] and after["worktree_removed"]
    else:
        [skip] = out["skipped"]
        assert "changed while cleanup checked it" in skip["reason"] and not after.get("worktree_removed")
        assert Path(d["worktree"]).is_dir()
    assert leases.list_all(droot) == []


def test_the_no_checks_gate_reads_head_with_no_records_guard_held(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    hold = Hold()
    monkeypatch.setattr(gate, "worktree_head", hold.wrap(gate.worktree_head))
    hold.during(
        lambda: gate.execute(droot, d["id"], 1), lambda: _write(droot, d["id"], written_during_head=True)
    )
    after = load(droot, d["id"])
    assert after["written_during_head"] and after["verification"]["mode"] == "exempt"


def test_the_no_checks_gate_publishes_nothing_for_an_attempt_that_went_stale(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    hold = Hold()
    monkeypatch.setattr(gate, "worktree_head", hold.wrap(gate.worktree_head))
    hold.during(lambda: gate.execute(droot, d["id"], 1), lambda: _write(droot, d["id"], state="cancelled"))
    assert load(droot, d["id"]).get("verification") is None
