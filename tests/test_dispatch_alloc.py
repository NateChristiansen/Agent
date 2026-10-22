"""atomic allocation under `dispatch-alloc:<short>`, the record mutex renamed to
`dispatch-records:<short>`, worktrees keyed by physical path, the allocation reconciler and the
cutover refusal. The reservation's life and `git-admin` are in `test_dispatch_alloc_life.py`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_cli import dispatch, leases, links
from agent_cli import features as features_mod
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, guards, notice_delivery
from agent_cli.dispatch import verification_gate as gate
from dispatch_helpers import DEAD_PID, _new_fast, droot, envelope, one, settle  # noqa: F401

RANK = {"dispatch-alloc": 0, "dispatch-records": 1, "git-admin": 2}


class GuardLog:
    """Every guard acquisition with the guards already held by that thread, through the real
    `area_guard`."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[str, ...], bool]] = []
        self._local = threading.local()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> GuardLog:
        real = guards.area_guard

        @contextmanager
        def recording(root, area, **kw):
            held = getattr(self._local, "held", ())
            from_leases = any(Path(f.filename).name == "leases.py" for f in traceback.extract_stack()[:-2])
            self.rows.append((area, held, from_leases))
            with real(root, area, **kw):
                self._local.held = (*held, area)
                try:
                    yield
                finally:
                    self._local.held = held

        for module in (guards, gate, allocation):
            monkeypatch.setattr(module, "area_guard", recording)
        return self

    def areas(self) -> set[str]:
        return {area for area, _, _ in self.rows}


def _feat(root: Path) -> features_mod.Feature:
    return features_mod.resolve(root, "1")


def _alpha(root: Path) -> repos_mod.Repo:
    return repos_mod.load_registry(root).require("alpha")


def _record(root: Path, dispatch_id: str, worktree: Path) -> dict:
    return {
        "id": dispatch_id,
        "repo": "alpha-api",
        "short": "alpha",
        "feature": 1,
        "state": "queued",
        "worktree": str(worktree),
        "created": "2026-10-04T20:00:00-06:00",
    }


def _no_detach(monkeypatch: pytest.MonkeyPatch, pid: int = DEAD_PID) -> None:
    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": pid, "__init__": lambda self, *a, **k: None})
    )


def _old_lease(root: Path, session_id: str = "dispatch:001-mac-77") -> None:
    directory = root / ".agent/leases"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "repo_alpha.json").write_text(
        json.dumps({"area": "repo:alpha", "session_id": session_id, "pid": os.getpid()})
    )


def test_concurrent_new_calls_get_distinct_ids_or_a_clean_refusal(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    barrier = threading.Barrier(2)
    results: list[object] = []

    def launch() -> None:
        barrier.wait(5)
        try:
            results.append(_new_fast(droot, watch=False))
        except AgentError as exc:
            results.append(exc)

    threads = [threading.Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    made = [r for r in results if isinstance(r, dict)]
    assert len(results) == 2 and made
    assert len({d["id"] for d in made}) == len(made)
    assert len({d["worktree"] for d in made}) == len(made)
    assert len({d["branch"] for d in made}) == len(made)
    records = sorted(p.name for p in (droot / ".agent/dispatches").glob("001-mac-??.json"))
    assert records == sorted(f"{d['id']}.json" for d in made)


def test_the_reuse_race_is_refused(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    finished = _new_fast(droot)
    feat, rp = _feat(droot), _alpha(droot)
    picks = []
    for _ in range(2):
        seq, dispatch_id = allocation.allocate(droot, feat, rp, "mac")
        picks.append((seq, dispatch_id, allocation.reusable(droot, feat, rp, "mac")))
    assert picks[0] == picks[1] and picks[0][2] == Path(finished["worktree"])
    first, second = (_record(droot, p[1], p[2]) for p in picks)
    allocation.claim(droot, first, feat, rp, "mac", seq=picks[0][0], reused=picks[0][2], escalated=False)
    with pytest.raises(AgentError, match="concurrent `dispatch new`"):
        allocation.claim(droot, second, feat, rp, "mac", seq=picks[1][0], reused=picks[1][2], escalated=False)
    assert dispatch.load(droot, first["id"])["worktree_key"] == os.path.realpath(finished["worktree"])


def test_a_held_worktree_is_refused_without_waiting(droot: Path, tmp_path):
    feat, rp = _feat(droot), _alpha(droot)
    shared = tmp_path / "shared"
    shared.mkdir()
    allocation.claim(
        droot, _record(droot, "001-mac-01", shared), feat, rp, "mac", seq=1, reused=None, escalated=True
    )
    leases.release(droot, "repo:alpha", "dispatch:001-mac-01")
    with pytest.raises(AgentError, match="is reserved by dispatch:001-mac-01"):
        allocation.claim(
            droot, _record(droot, "001-mac-02", shared), feat, rp, "mac", seq=2, reused=None, escalated=True
        )
    assert not (droot / ".agent/dispatches/001-mac-02.json").exists()


def test_a_symlinked_worktree_cannot_be_allocated_twice(droot: Path, tmp_path):
    feat, rp = _feat(droot), _alpha(droot)
    real = tmp_path / "real-tree"
    real.mkdir()
    alias = tmp_path / "alias-tree"
    links.make(alias, real, is_dir=True)
    assert allocation.worktree_key(alias) == allocation.worktree_key(real)
    assert allocation.worktree_area("alpha", alias) == "worktree:alpha/real-tree"
    allocation.claim(
        droot, _record(droot, "001-mac-01", real), feat, rp, "mac", seq=1, reused=None, escalated=True
    )
    leases.release(droot, "repo:alpha", "dispatch:001-mac-01")
    with pytest.raises(AgentError, match="is reserved"):
        allocation.claim(
            droot, _record(droot, "001-mac-02", alias), feat, rp, "mac", seq=2, reused=None, escalated=True
        )


def test_the_record_and_its_leases_carry_the_protocol_and_worktree_key(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    record = dispatch.load(droot, d["id"])
    key = os.path.realpath(d["worktree"])
    assert record["worktree_key"] == key and record["lease_protocol"] == 2
    assert record["slot"] == 0 and record["slot_schema"] == 1
    assert leases.get(droot, "repo:alpha") is None
    slot = leases.get(droot, "repo-slot:alpha/0")
    assert slot is not None and slot["lease_protocol"] == 2
    reservation = leases.get(droot, allocation.worktree_area("alpha", key))
    assert reservation is not None and reservation["worktree_key"] == key
    assert reservation["lease_protocol"] == 2 and reservation["session_id"] == f"dispatch:{d['id']}"


def test_every_record_write_takes_dispatch_records(droot: Path, fake_claude, monkeypatch):
    log = GuardLog().install(monkeypatch)
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    with notice_delivery.bounded_record_guard(droot, d["id"], 1.0):
        pass
    dispatch.cleanup(droot, feature="1")
    assert {"dispatch-alloc:alpha", "dispatch-records:alpha", "git-admin:alpha"} <= log.areas()
    assert log.rows[-1][0] != "repo:alpha"
    for area, held, from_leases in log.rows:
        if area.startswith(("repo:", "worktree:")):
            assert from_leases, f"{area} taken as a record guard outside leases.py"
            continue
        rank = RANK.get(area.split(":")[0])
        if rank is not None:
            assert all(RANK.get(h.split(":")[0], -1) <= rank for h in held), (area, held)
    creation = [
        held
        for area, held, _ in log.rows
        if area == "dispatch-records:alpha" and "dispatch-alloc:alpha" in held
    ]
    assert creation, "record creation is not nested inside dispatch-alloc"


def test_record_guard_and_save_name_dispatch_records(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    log = GuardLog().install(monkeypatch)
    with gate.record_guard(droot, d["id"]) as current:
        dispatch.save(droot, current)
    assert [area for area, _, _ in log.rows] == ["dispatch-records:alpha", "dispatch-records:alpha"]


def test_a_short_guard_times_out_with_its_name(droot: Path):
    hold, done = threading.Event(), threading.Event()

    def holder() -> None:
        with allocation.short_guard(droot, "dispatch-alloc:alpha"):
            hold.set()
            done.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    hold.wait(5)
    try:
        with pytest.raises(AgentError, match=r"dispatch-alloc:alpha is busy past 3 s"):
            with allocation.short_guard(droot, "dispatch-alloc:alpha"):
                pytest.fail("acquired a held guard")
    finally:
        done.set()
        thread.join(10)


def test_no_lifetime_lease_wait_happens_under_a_short_guard(droot: Path, tmp_path):
    """Another thread holds the worktree lease live. Under the alloc guard the take refuses at
    once; a waiting take would sit until the 3 s guard deadline of a second caller."""
    feat, rp = _feat(droot), _alpha(droot)
    tree = tmp_path / "tree"
    tree.mkdir()
    leases.take_marked(droot, allocation.worktree_area("alpha", tree), session_id="someone", pid=os.getpid())
    outcome: list[object] = []

    def second() -> None:
        try:
            with allocation.short_guard(droot, "dispatch-alloc:alpha"):
                outcome.append("entered")
        except AgentError as exc:
            outcome.append(exc)

    with pytest.raises(AgentError, match="is reserved by someone"):
        allocation.claim(
            droot, _record(droot, "001-mac-01", tree), feat, rp, "mac", seq=1, reused=None, escalated=True
        )
    thread = threading.Thread(target=second)
    thread.start()
    thread.join(5)
    assert outcome == ["entered"]


def _crash_after_reservation(root: Path, tree: Path) -> None:
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from agent_cli.dispatch import holdings\n"
        "d = {'id': '001-mac-09', 'short': 'alpha', 'worktree': sys.argv[2]}\n"
        "holdings.acquire(Path(sys.argv[1]), d, limit=1)\n"
        "os._exit(1)\n"
    )
    code = subprocess.run([sys.executable, "-c", script, str(root), str(tree)], check=False).returncode
    assert code == 1


def test_a_crash_before_the_record_is_reconciled(droot: Path, tmp_path):
    tree = tmp_path / "crashed"
    tree.mkdir()
    _crash_after_reservation(droot, tree)
    areas = ["repo-slot:alpha/0", allocation.worktree_area("alpha", tree)]
    assert all(leases.get(droot, area) is not None for area in areas)
    assert sorted(allocation.reconcile_allocations(droot)) == sorted(areas)
    assert all(leases.get(droot, area) is None for area in areas)


def test_a_record_or_a_live_process_keeps_its_reservation(droot: Path, tmp_path):
    kept = []
    for name, pid, record in (
        ("recorded", DEAD_PID, True),
        ("pending", DEAD_PID, True),
        ("live", os.getpid(), False),
    ):
        tree = tmp_path / name
        tree.mkdir()
        dispatch_id = f"001-mac-{len(kept) + 10}"
        area = allocation.worktree_area("alpha", tree)
        leases.take_marked(droot, area, session_id=f"dispatch:{dispatch_id}", pid=pid)
        if record:
            d = {
                **_record(droot, dispatch_id, tree),
                "state": "done",
                "verification_cleanup_pending": name == "pending",
            }
            dispatch.save(droot, d)
        kept.append(area)
    assert allocation.reconcile_allocations(droot) == []
    assert all(leases.get(droot, area) is not None for area in kept)


def test_the_allocation_reconciler_never_raises(droot: Path, monkeypatch):
    monkeypatch.setattr(allocation.leases_mod, "list_all", lambda root: 1 / 0)
    assert allocation.reconcile_allocations(droot) == []


def test_an_old_dispatch_lease_refuses_new(droot: Path):
    _old_lease(droot)
    with pytest.raises(AgentError, match="dispatch:001-mac-77 without lease_protocol 2"):
        _new_fast(droot)
    assert not list((droot / ".agent/dispatches").glob("*.json"))


def test_an_old_dispatch_lease_refuses_resume(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    settle(droot, d)
    _old_lease(droot)
    with pytest.raises(AgentError, match="without lease_protocol 2"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="again", watch=True)


def test_a_marked_lease_is_refused_only_as_a_held_lease(droot: Path):
    leases.take_marked(droot, "repo:alpha", session_id="dispatch:001-mac-77", pid=os.getpid())
    with pytest.raises(AgentError) as refused:
        _new_fast(droot)
    assert "lease_protocol" not in str(refused.value) and "is held by" in str(refused.value)
    assert "dispatch:001-mac-77" in str(refused.value) and "without a repo slot" in str(refused.value)
    assert not list((droot / ".agent/dispatches").glob("*.json"))


def _finished(droot: Path, fake_claude, **fields) -> dict:
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    d.update(fields)
    dispatch.save(droot, d)
    return d


def _lose(path: Path) -> None:
    from agent_cli import worktree

    worktree.unlink_links(path)
    shutil.rmtree(path)


@pytest.mark.parametrize(
    "fields",
    [
        {"setup": [{"cmd": "npm ci", "exit": 1, "seconds": 0.1}]},
        {"setup_pending": True},
        {"worker_cleanup_pending": True},
    ],
    ids=["setup-failed", "setup-unfinished", "cleanup-pending"],
)
def test_a_tree_whose_setup_failed_or_cleanup_is_pending_is_not_reused(droot: Path, fake_claude, fields):
    d = _finished(droot, fake_claude)
    feat, rp = _feat(droot), _alpha(droot)
    assert allocation.reusable(droot, feat, rp, "mac") == Path(d["worktree"])
    d.update(fields)
    dispatch.save(droot, d)
    assert allocation.reusable(droot, feat, rp, "mac") is None


def test_an_unregistered_directory_at_a_recorded_path_is_not_reused_or_replaced(droot: Path, fake_claude):
    """The supported failed-launch case: the allocation path was retained after setup failed and
    Git has no registration for it. Nothing is reused, deleted or reported as created."""
    d = _finished(droot, fake_claude)
    wt = Path(d["worktree"])
    rp = _alpha(droot)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=rp.abs_path(droot), check=True)
    wt.mkdir()
    (wt / "partial-install.log").write_text("setup output\n")
    assert allocation.reusable(droot, _feat(droot), rp, "mac") is None
    for resume in (False, True):
        with pytest.raises(AgentError, match="kept as it is"):
            dispatch.worktrees.ensure_worktree(droot, rp, wt, d["branch"], d["base"], resume=resume)
    assert (wt / "partial-install.log").read_text() == "setup output\n"


def test_a_registration_alone_is_not_a_checkout(droot: Path, fake_claude):
    d = _finished(droot, fake_claude)
    wt = Path(d["worktree"])
    rp = _alpha(droot)
    main = rp.abs_path(droot)
    other = main.parent / "other-lost"
    subprocess.run(["git", "worktree", "add", "-b", "other-lost", str(other)], cwd=main, check=True)
    _lose(other)
    _lose(wt)
    assert allocation.reusable(droot, _feat(droot), rp, "mac") is None
    with pytest.raises(AgentError, match="registered in alpha but its checkout is not there"):
        dispatch.worktrees.ensure_worktree(droot, rp, wt, d["branch"], d["base"])
    created = dispatch.worktrees.ensure_worktree(droot, rp, wt, d["branch"], d["base"], resume=True)
    assert created["created"] and wt.is_dir()
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=main, capture_output=True, text=True, check=True
    ).stdout
    assert str(other) in listed  # scoped: the other missing registration was not pruned
