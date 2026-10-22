"""the worktree reservation's life beside the `repo:<short>` worker lease, the
`lease_protocol: 2` marker across re-takes, `git-admin:<short>` and where the allocation
reconciler runs. Allocation itself is in `test_dispatch_alloc.py`."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from agent_cli import dispatch, leases, worktree
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, lifecycle, recovery, supervisor
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch.worktrees import ensure_worktree
from dispatch_helpers import DEAD_PID, _new, _new_fast, droot, envelope, one, settle  # noqa: F401
from test_dispatch_alloc import GuardLog, _no_detach, _old_lease
from test_dispatch_reverify import _blocked_by_dirt
from test_dispatch_verification import ready  # noqa: F401


def _reservation(root: Path, d: dict) -> dict | None:
    return leases.get(root, allocation.worktree_area("alpha", d["worktree"]))


def _held(root: Path, area: str = "repo-slot:alpha/0") -> dict:
    lease = leases.get(root, area)
    assert lease is not None
    return lease


def test_terminal_commit_releases_the_reservation(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["state"] == "done"
    assert leases.get(droot, "repo-slot:alpha/0") is None and _reservation(droot, d) is None


def test_recovery_releases_the_reservation(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    assert _reservation(droot, d) is not None
    [row] = dispatch.collect(droot, dispatch_id=d["id"])
    assert row["state"] == "failed"
    assert leases.get(droot, "repo-slot:alpha/0") is None and _reservation(droot, d) is None


def test_cancel_releases_the_reservation(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    dispatch.cancel(droot, dispatch_id=d["id"])
    assert leases.get(droot, "repo-slot:alpha/0") is None and _reservation(droot, d) is None


def test_a_failed_setup_leaves_no_record_and_no_lease(droot: Path, monkeypatch):
    def broken(*args, **kwargs):
        raise AgentError("worktree add failed")

    monkeypatch.setattr(lifecycle, "ensure_worktree", broken)
    with pytest.raises(AgentError, match="worktree add failed"):
        _new_fast(droot)
    assert not list((droot / ".agent/dispatches").glob("001-mac-??.json"))
    assert leases.list_all(droot) == []


def test_resume_retakes_the_reservation_with_the_worker_lease(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope("question"))])
    d = _new_fast(droot)
    assert _reservation(droot, d) is None
    _no_detach(monkeypatch)
    dispatch.resume(droot, dispatch_id=d["id"], answer="use port 8080", watch=False)
    held = _reservation(droot, d)
    assert held is not None and held["session_id"] == f"dispatch:{d['id']}"
    assert held["lease_protocol"] == 2 and _held(droot)["lease_protocol"] == 2
    assert dispatch.load(droot, d["id"])["lease_protocol"] == 2


def test_a_stale_reservation_never_refuses_what_the_worker_lease_allows(droot: Path, fake_claude):
    """A finished dispatch's reservation left behind (a failed best-effort release) does not
    stop the next dispatch reusing that worktree: its holder would not hold the worker lease."""
    fake_claude.scenario([one(envelope())])
    d1 = _new_fast(droot)
    settle(droot, d1)
    leases.take_marked(
        droot,
        allocation.worktree_area("alpha", d1["worktree"]),
        session_id=f"dispatch:{d1['id']}",
        pid=DEAD_PID,
    )
    d2 = _new_fast(droot)
    assert d2["reused"] is True and d2["worktree"] == d1["worktree"] and d2["state"] == "done"


def test_the_marker_survives_every_retake(droot: Path, fake_claude, monkeypatch):
    marks: list[object] = []
    real = leases.take

    def spy(root, area, **kw):
        if area.startswith(("repo-slot:", "worktree:")):
            marks.append((kw.get("marks") or {}).get("lease_protocol"))
        return real(root, area, **kw)

    monkeypatch.setattr(leases, "take", spy)
    run_worker = supervisor.run_worker
    during: list[object] = []

    def inspect(*args, **kwargs):
        during.append(_held(droot)["lease_protocol"])
        return run_worker(*args, **kwargs)

    monkeypatch.setattr(supervisor, "run_worker", inspect)
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    d = _new(droot)
    assert d["state"] == "done" and d["verification_result"]["status"] == "verified"
    # new, the supervisor, the verifier and the terminal re-take before acceptance
    assert len(marks) >= 4 and set(marks) == {2} and during == [2]


def test_concurrent_add_and_prune_leave_git_worktrees_intact(droot: Path):
    rp = repos_mod.load_registry(droot).require("alpha")
    main = rp.abs_path(droot)
    paths = [droot / "repos/_worktrees/alpha" / f"k1-{i}" for i in range(4)]
    errors: list[BaseException] = []

    def add() -> None:
        try:
            for i, path in enumerate(paths):
                ensure_worktree(droot, rp, path, f"feature/k1-{i}", f"origin/{rp.default_branch}")
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assertion below
            errors.append(exc)

    def prune() -> None:
        for _ in range(30):
            with allocation.git_admin(droot, "alpha"):
                repos_mod.git_or_none(["worktree", "prune"], main)

    threads = [threading.Thread(target=add), threading.Thread(target=prune)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=main, capture_output=True, text=True, check=True
    ).stdout
    # git prints forward slashes on Windows too
    assert all(path.resolve().as_posix() in listing or path.as_posix() in listing for path in paths)
    assert len(list((main / ".git/worktrees").iterdir())) == len(paths)


def test_the_manager_worktree_tool_takes_git_admin(droot: Path, monkeypatch):
    rp = repos_mod.load_registry(droot).require("alpha")
    main = rp.abs_path(droot)
    assert allocation.git_admin_short(droot, main) == "alpha"
    log = GuardLog().install(monkeypatch)
    short = allocation.git_admin_short(main, main)
    created = worktree.create(main, "k1", sync=False)
    worktree.remove(main, "k1", force=True)
    assert Path(created["path"]).name == "k1"
    assert [area for area, _, _ in log.rows if area.startswith("git-admin:")] == [f"git-admin:{short}"] * 2


def _spy_reconcilers(monkeypatch, module, *, notice_raises: bool = False) -> list[str]:
    order: list[str] = []
    monkeypatch.setattr(module, "reconcile_allocations", lambda root, **kw: order.append("allocations") or [])

    def notices(*args, **kwargs):
        order.append("notices")
        if notice_raises:
            raise RuntimeError("notice reconcile failed")
        return []

    monkeypatch.setattr(module, "reconcile", notices)
    return order


def test_the_allocation_reconciler_runs_at_new(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    calls: list[Path] = []
    monkeypatch.setattr(allocation, "reconcile_allocations", lambda root, **kw: calls.append(root) or [])
    _new_fast(droot, watch=False)
    assert calls == [droot]


def test_both_reconcilers_run_at_collect_even_when_the_notice_one_raises(droot: Path, monkeypatch):
    order = _spy_reconcilers(monkeypatch, recovery, notice_raises=True)
    with pytest.raises(RuntimeError, match="notice reconcile failed"):
        dispatch.collect(droot)
    assert order == ["allocations", "notices"]


def test_the_notice_reconciler_runs_when_the_allocation_pass_fails(droot: Path, monkeypatch):
    monkeypatch.setattr(allocation.leases_mod, "list_all", lambda root: 1 / 0)
    seen: list[str] = []
    monkeypatch.setattr(recovery, "reconcile", lambda *a, **k: seen.append("notices") or [])
    assert dispatch.collect(droot) == []
    assert seen == ["notices"]


def test_both_reconcilers_run_at_supervisor_start(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    order = _spy_reconcilers(monkeypatch, supervisor)
    with pytest.raises(AgentError, match="not queued"):
        supervisor.run(droot, dispatch_id=d["id"])
    assert order == ["allocations", "notices"]


def test_an_old_dispatch_lease_refuses_reverify(ready):
    root, d = ready
    _blocked_by_dirt(root, d)
    _old_lease(root)
    with pytest.raises(AgentError, match="without lease_protocol 2"):
        reverify_mod.reverify(root, d["id"])
    assert _held(root, "repo:alpha")["session_id"] == "dispatch:001-mac-77"
    assert "lease_protocol" not in _held(root, "repo:alpha")
