"""numbered repo slots under `dispatch.max_concurrent`, the stored slot across
every path, allocation races, records without a slot (K0 addendum A2), the cutover refusal and
the preview. Retention and reverify's interim slot are in `test_dispatch_holdings.py`."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import dispatch, leases, links, procs
from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, holdings, lifecycle, state
from agent_cli.dispatch import verification_gate as gate
from dispatch_helpers import _new_fast, _set_alpha_dispatch, droot, envelope, one  # noqa: F401
from test_dispatch_alloc import _no_detach
from test_dispatch_alloc_live import _proc

SLOT0, SLOT1 = "repo-slot:alpha/0", "repo-slot:alpha/1"


def _held_by(root: Path, dispatch_id: str) -> list[str]:
    return sorted(
        str(x["area"]) for x in leases.list_all(root) if x.get("session_id") == f"dispatch:{dispatch_id}"
    )


def _foreign(root: Path, area: str) -> None:
    leases.take_marked(root, area, session_id="someone", pid=os.getpid())


def _trusted(monkeypatch: pytest.MonkeyPatch, *rows: procs.Proc) -> None:
    monkeypatch.setattr(procs, "snapshot", lambda: [_proc(os.getpid()), *rows])


def test_area_grammar_takes_a_slot_number():
    from agent_cli.lease_keys import area_key

    assert area_key("repo-slot:alpha/0") == "repo-slot_alpha~0"
    assert area_key("repo-slot:alpha/12") == "repo-slot_alpha~12"
    for bad in (
        "repo-slot:alpha",
        "repo-slot:alpha/",
        "repo-slot:/alpha/0",
        "repo-slot:alpha/0/1",
        "repo-slot:alpha//0",
        "repo-slot:alpha/01",
        "repo-slot:alpha/-1",
        "repo-slot:alpha/x",
    ):
        with pytest.raises(AgentError, match="noncanonical"):
            area_key(bad)
    keys = {area_key(a) for a in ("repo-slot:a/1", "repo-slot:a1/0", "worktree:a/1", "repo:a", "vault:a/1")}
    assert len(keys) == 5
    assert not leases.conflicts("repo-slot:a/1", "repo-slot:a/10") and not leases.conflicts(
        "repo-slot:a/1", "repo:a"
    )


def test_cap_one_refuses_a_second_dispatch_like_the_repo_lease(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    _set_alpha_dispatch(droot, max_concurrent=1)
    first = _new_fast(droot, watch=False)
    assert first["slot"] == 0 and first["slot_schema"] == 1
    with pytest.raises(AgentError, match=r"repo-slot:alpha/0 is held by dispatch:001-mac-01"):
        _new_fast(droot, watch=False)
    assert not (droot / ".agent/dispatches/001-mac-02.json").exists()
    assert _held_by(droot, "001-mac-02") == []


def test_a_cap_of_two_admits_two_and_refuses_a_third(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    _set_alpha_dispatch(droot, max_concurrent=2)
    made = [_new_fast(droot, watch=False) for _ in range(2)]
    assert [d["slot"] for d in made] == [0, 1] and len({d["worktree"] for d in made}) == 2
    with pytest.raises(AgentError, match="every repo slot for alpha is held"):
        _new_fast(droot, watch=False)
    assert not (droot / ".agent/dispatches/001-mac-03.json").exists()


def test_max_concurrent_is_validated(droot: Path):
    from agent_cli.repos_validate import validate_dispatch

    for bad in (0, -1, True, 1.5, "2"):
        assert "dispatch.max_concurrent must be a positive integer" in validate_dispatch(
            {"max_concurrent": bad}
        )
    assert validate_dispatch({"max_concurrent": 3}) == []


def test_the_stored_slot_is_kept_across_terminal_refusal_resume_and_recovery(
    droot: Path, fake_claude, monkeypatch
):
    _set_alpha_dispatch(droot, max_concurrent=2)
    _foreign(droot, SLOT0)
    fake_claude.scenario([one(envelope("question")), one(envelope())])
    d = _new_fast(droot)
    assert d["state"] == "question" and d["slot"] == 1 and _held_by(droot, d["id"]) == []
    leases.release(droot, SLOT0, "someone")
    _foreign(droot, SLOT1)
    before = dispatch.file(droot, d["id"], ".json").read_bytes()
    with pytest.raises(AgentError, match=r"repo-slot:alpha/1 is held by someone"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="go", watch=True)
    assert dispatch.file(droot, d["id"], ".json").read_bytes() == before
    assert leases.get(droot, SLOT0) is None and _held_by(droot, d["id"]) == []
    leases.release(droot, SLOT1, "someone")
    taken: list[str] = []
    real = leases.take

    def spy(root, area, **kw):
        if kw.get("session_id") == f"dispatch:{d['id']}" and area.startswith("repo-slot:"):
            taken.append(area)
        return real(root, area, **kw)

    monkeypatch.setattr(leases, "take", spy)
    done = dispatch.resume(droot, dispatch_id=d["id"], answer="go", watch=True)
    assert done["state"] == "done" and done["slot"] == 1 and set(taken) == {SLOT1}
    _no_detach(monkeypatch)
    queued = _new_fast(droot, watch=False)
    assert queued["slot"] == 0
    [row] = dispatch.collect(droot, dispatch_id=queued["id"])
    assert row["state"] == "failed" and dispatch.load(droot, queued["id"])["slot"] == 0
    assert _held_by(droot, queued["id"]) == []


def test_a_slot_refusal_rolls_back_only_the_new_worktree_lease(droot: Path):
    _set_alpha_dispatch(droot, max_concurrent=1)
    _foreign(droot, SLOT0)
    with pytest.raises(AgentError, match="repo-slot:alpha/0 is held by someone"):
        _new_fast(droot)
    assert [x["area"] for x in leases.list_all(droot)] == [SLOT0]
    assert not list((droot / ".agent/dispatches").glob("001-*.json"))


def test_an_alias_names_the_same_worktree_lease(droot: Path, tmp_path):
    real = tmp_path / "real-tree"
    real.mkdir()
    alias = tmp_path / "alias-tree"
    links.make(alias, real, is_dir=True)
    first = {"id": "001-mac-01", "repo": "alpha-api", "short": "alpha", "worktree": str(real)}
    holdings.acquire(droot, first, limit=2)
    with pytest.raises(AgentError, match="is reserved by dispatch:001-mac-01"):
        holdings.acquire(droot, {**first, "id": "001-mac-02", "worktree": str(alias)}, limit=2)
    assert leases.get(droot, SLOT1) is None and _held_by(droot, "001-mac-02") == []


def test_reuse_and_resume_cannot_both_take_one_worktree(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    first = _new_fast(droot)
    _no_detach(monkeypatch)
    _set_alpha_dispatch(droot, max_concurrent=2)
    second = _new_fast(droot, watch=False)
    assert second["reused"] and second["worktree"] == first["worktree"]
    before = dispatch.file(droot, first["id"], ".json").read_bytes()
    with pytest.raises(AgentError, match=f"is reserved by dispatch:{second['id']}"):
        dispatch.resume(droot, dispatch_id=first["id"], answer="again", watch=False)
    assert dispatch.file(droot, first["id"], ".json").read_bytes() == before
    assert _held_by(droot, first["id"]) == []


def test_a_second_resume_of_one_dispatch_refuses_and_keeps_the_holdings(
    droot: Path, fake_claude, monkeypatch
):
    fake_claude.scenario([one(envelope("question"))])
    d = _new_fast(droot)
    _no_detach(monkeypatch)
    dispatch.resume(droot, dispatch_id=d["id"], answer="one", watch=False)
    held = _held_by(droot, d["id"])
    with pytest.raises(AgentError, match="is queued"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="two", watch=False)
    assert _held_by(droot, d["id"]) == held and len(held) == 2


RACER = """import sys, time
from pathlib import Path
from agent_cli.core import AgentError
from agent_cli.dispatch import holdings
root, ident, tree, go = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
while not go.exists():
    time.sleep(0.005)
try:
    holdings.acquire(root, {"id": ident, "repo": "alpha-api", "short": "alpha", "worktree": tree}, limit=1)
    print("won", flush=True)
except AgentError:
    print("refused", flush=True)
while not Path(str(go) + ".done").exists():
    time.sleep(0.005)
"""


def test_the_last_slot_race_admits_exactly_one_process(droot: Path, tmp_path):
    go = tmp_path / "go"
    racers = []
    for n in (1, 2):
        tree = tmp_path / f"tree-{n}"
        tree.mkdir()
        args = [sys.executable, "-c", RACER, str(droot), f"001-mac-0{n}", str(tree), str(go)]
        racers.append(subprocess.Popen(args, stdout=subprocess.PIPE, text=True))
    go.touch()
    try:
        outcomes = sorted(p.stdout.readline().strip() for p in racers if p.stdout is not None)
    finally:
        (tmp_path / "go.done").touch()
        for p in racers:
            p.communicate(timeout=60)
    assert outcomes == ["refused", "won"]
    slot = leases.get(droot, SLOT0)
    assert slot is not None and slot["session_id"] in ("dispatch:001-mac-01", "dispatch:001-mac-02")


def test_an_orphan_slot_is_reconciled_only_without_a_record(droot: Path, monkeypatch):
    leases.take_marked(droot, SLOT0, session_id="dispatch:001-mac-09", pid=2**22)
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    assert d["slot"] == 0 and (leases.get(droot, SLOT0) or {}).get("session_id") == f"dispatch:{d['id']}"
    leases.take_marked(droot, SLOT1, session_id=f"dispatch:{d['id']}", pid=2**22)
    assert SLOT1 not in allocation.reconcile_allocations(droot)


def _legacy(root: Path, d: dict, state_name: str, generation: str) -> dict:
    with gate.record_guard(root, d["id"]) as current:
        for key in ("slot", "slot_schema") + (
            ("lease_protocol", "worktree_key") if generation == "pre-marker" else ()
        ):
            current.pop(key, None)
        current["state"] = state_name
        state.save(root, current)
    return current


# Generation (the lease protocol a record was written under) and terminal state are independent
# axes of the legacy migration, so each is covered once along the other's default instead of as a
# 2 x 5 grid of full dispatch-and-resume runs (verify speed, phase 3).
LEGACY_CASES = [("pre-marker", "done")] + [
    ("k1", s) for s in ("done", "blocked", "failed", "question", "cancelled")
]


@pytest.mark.parametrize(("generation", "state_name"), LEGACY_CASES)
def test_a_legacy_record_resumes_into_a_fresh_slot(
    droot: Path, fake_claude, monkeypatch, generation, state_name
):
    fake_claude.scenario([one(envelope())])
    d = _legacy(droot, _new_fast(droot), state_name, generation)
    _trusted(monkeypatch)
    _no_detach(monkeypatch)
    saves: list[dict] = []
    real = lifecycle.save
    monkeypatch.setattr(lifecycle, "save", lambda root, rec: saves.append(dict(rec)) or real(root, rec))
    dispatch.resume(droot, dispatch_id=d["id"], answer="again", watch=False)
    record = dispatch.load(droot, d["id"])
    assert record["slot"] == 0 and record["slot_schema"] == 1 and record["lease_protocol"] == 2
    assert [(s["slot"], s["slot_schema"], s["state"]) for s in saves] == [(0, 1, "queued")]
    assert _held_by(droot, d["id"]) == sorted([SLOT0, allocation.worktree_area("alpha", d["worktree"])])


def test_a_legacy_record_with_a_possible_live_process_refuses(droot: Path, fake_claude, monkeypatch):
    """An untrusted inventory, then a trusted one naming a descendant: the refusal changes
    nothing, so one legacy record serves both."""
    fake_claude.scenario([one(envelope())])
    d = _legacy(droot, _new_fast(droot), "done", "k1")
    before = dispatch.file(droot, d["id"], ".json").read_bytes()
    for inventory in ("untrusted", "descendant"):
        if inventory == "descendant":
            _trusted(monkeypatch, _proc(4242, dispatch=d["id"], root=str(droot)))
        with pytest.raises(AgentError, match="predates repo slots"):
            dispatch.resume(droot, dispatch_id=d["id"], answer="again", watch=False)
        assert dispatch.file(droot, d["id"], ".json").read_bytes() == before and leases.list_all(droot) == []


def _windows(monkeypatch: pytest.MonkeyPatch, *rows: procs.Proc) -> None:
    """A Windows inventory with an unrelated row whose environment cannot be read and that started
    after the dispatch, like a Codex or node child of another session."""
    unreadable = procs.Proc(
        pid=9001,
        ppid=9000,
        pgid=9001,
        elapsed="1",
        command="node",
        tag_error="AccessDenied",
        created=time.time() + 60,
    )
    monkeypatch.setattr(allocation, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(allocation, "_inventory", lambda: [unreadable, *rows])


def _slot_given_back(root: Path, fake_claude) -> dict:
    fake_claude.scenario([one(envelope())])
    d = _new_fast(root)
    assert d["slot_schema"] == 1 and d["slot"] == 0
    assert holdings.release_slot(root, d["id"], d["attempt"])
    record = dispatch.load(root, d["id"])
    assert "slot_schema" not in record and holdings.slot_released(record)
    return record


def test_a_dispatch_that_gave_its_slot_back_refuses_while_its_own_process_runs_then_resumes(
    droot: Path, fake_claude, monkeypatch
):
    """Resumes beside an unreadable Windows row once its own process is gone."""
    d = _slot_given_back(droot, fake_claude)
    before = dispatch.file(droot, d["id"], ".json").read_bytes()
    with monkeypatch.context() as m:
        _windows(m, _proc(4242, dispatch=d["id"], root=str(droot)))
        with pytest.raises(AgentError, match=r"gave its repo slot back and its processes \[4242\] still run"):
            dispatch.resume(droot, dispatch_id=d["id"], answer="add line two", watch=False)
    assert dispatch.file(droot, d["id"], ".json").read_bytes() == before and _held_by(droot, d["id"]) == []
    _windows(monkeypatch)
    assert allocation.owned_pids(droot, d) == ([], False)
    _no_detach(monkeypatch)
    dispatch.resume(droot, dispatch_id=d["id"], answer="add line two", watch=False)
    record = dispatch.load(droot, d["id"])
    assert record["attempt"] == 2 and record["slot"] == 0 and record["slot_schema"] == 1


def _running_after_reap(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A running dispatch whose supervisor confirmed its worker gone and then exited."""
    _no_detach(monkeypatch)
    d = _new_fast(root, watch=False)
    with gate.record_guard(root, d["id"]) as current:
        current.update(
            state="running",
            attempt_started=current.get("created"),
            claude_pid=None,
            worker_cleanup_pending=False,
            worker_cleanup_confirmed=True,
            stop_reason="cancelled",
        )
        state.save(root, current)
    assert _held_by(root, d["id"]) == sorted([SLOT0, allocation.worktree_area("alpha", d["worktree"])])
    return current


def test_cancel_frees_the_worktree_lease_once_its_processes_are_gone(droot: Path, monkeypatch):
    d = _running_after_reap(droot, monkeypatch)
    _windows(monkeypatch)
    assert allocation.owned_pids(droot, d) == ([], False)
    assert dispatch.cancel(droot, dispatch_id=d["id"])["state"] == "cancelled"
    assert not dispatch.load(droot, d["id"]).get("holdings_held") and _held_by(droot, d["id"]) == []
    for dry_run in (True, False):
        dispatch.cleanup(droot, feature="1", dry_run=dry_run)


def test_cancel_keeps_the_worktree_lease_while_a_process_of_its_own_survives(droot: Path, monkeypatch):
    d = _running_after_reap(droot, monkeypatch)
    _windows(monkeypatch, _proc(4242, dispatch=d["id"], root=str(droot)))
    assert dispatch.cancel(droot, dispatch_id=d["id"])["state"] == "cancelled"
    assert dispatch.load(droot, d["id"])["holdings_held"]["pids"] == [4242]
    assert allocation.worktree_area("alpha", d["worktree"]) in _held_by(droot, d["id"])
    for dry_run in (True, False):
        with pytest.raises(AgentError, match=f"is held by dispatch:{d['id']}"):
            dispatch.cleanup(droot, feature="1", dry_run=dry_run)


@pytest.mark.parametrize("inventory", ["unreadable child of the supervisor", "empty"])
def test_a_confirmed_cleanup_does_not_excuse_a_suspect_or_missing_inventory(
    droot: Path, monkeypatch, inventory
):
    d = _running_after_reap(droot, monkeypatch)
    if inventory == "empty":
        _windows(monkeypatch)
        monkeypatch.setattr(allocation, "_inventory", lambda: [])
        pids = []
    else:
        child = procs.Proc(
            pid=4343,
            ppid=d["supervisor_pid"],
            pgid=4343,
            elapsed="1",
            command="node",
            tag_error="AccessDenied",
            created=time.time() + 60,
        )
        _windows(monkeypatch, child)
        pids = [4343]
    dispatch.cancel(droot, dispatch_id=d["id"])
    assert dispatch.load(droot, d["id"])["holdings_held"]["pids"] == pids
    with pytest.raises(AgentError, match=f"is held by dispatch:{d['id']}"):
        dispatch.cleanup(droot, feature="1", dry_run=True)


def test_a_failed_save_rolls_back_only_what_resume_took(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    d = _legacy(droot, _new_fast(droot), "done", "k1")
    _trusted(monkeypatch)
    leases.take_marked(droot, "worktree:alpha/unrelated", session_id="someone", pid=os.getpid())

    def broken(root, rec):
        raise AgentError("disk full")

    monkeypatch.setattr(lifecycle, "save", broken)
    with pytest.raises(AgentError, match="disk full"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="again", watch=False)
    assert [x["area"] for x in leases.list_all(droot)] == ["worktree:alpha/unrelated"]
    assert "slot_schema" not in dispatch.load(droot, d["id"])


def test_a_malformed_record_refuses_by_name(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    for slot in (None, 5, "0", -1):
        with gate.record_guard(droot, d["id"]) as current:
            current["slot"] = slot
            state.save(droot, current)
        with pytest.raises(AgentError, match=f"dispatch {d['id']} has slot_schema but no valid slot"):
            dispatch.resume(droot, dispatch_id=d["id"], answer="again", watch=False)
        assert leases.list_all(droot) == [], slot


def test_a_malformed_running_record_recovers_through_cancel_and_collect(droot: Path, monkeypatch):
    _no_detach(monkeypatch)
    d = _new_fast(droot, watch=False)
    with gate.record_guard(droot, d["id"]) as current:
        current.pop("slot")
        state.save(droot, current)
    assert dispatch.cancel(droot, dispatch_id=d["id"])["state"] == "cancelled"
    dispatch.collect(droot, dispatch_id=d["id"])
    assert _held_by(droot, d["id"]) == []
    assert _new_fast(droot, watch=False)["slot"] == 0


def test_a_malformed_done_record_recovers_through_collect_abandon_and_new(
    droot: Path, fake_claude, monkeypatch
):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    with gate.record_guard(droot, d["id"]) as current:
        current["slot"] = "lost"
        state.save(droot, current)
    holdings.acquire(droot, {**d, "slot_schema": 1, "slot": 0}, note="left behind")
    leases.take_marked(droot, SLOT1, session_id="dispatch:001-mac-77", pid=os.getpid())
    with pytest.raises(AgentError, match="has slot_schema but no valid slot"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="again", watch=False)
    dispatch.collect(droot, dispatch_id=d["id"])
    with gate.record_guard(droot, d["id"]) as current:
        current["stop_reason"], current["worker_cleanup_confirmed"] = "cleanup_failed", False
        state.save(droot, current)
    with pytest.raises(AgentError, match="cleanup is incomplete"):
        dispatch.abandon(droot, dispatch_id=d["id"], note="lost its slot")
    assert len(_held_by(droot, d["id"])) == 2
    with gate.record_guard(droot, d["id"]) as current:
        current["worker_cleanup_confirmed"] = True
        state.save(droot, current)
    assert dispatch.abandon(droot, dispatch_id=d["id"], note="lost its slot")["state"] == "abandoned"
    assert _held_by(droot, d["id"]) == []
    assert (leases.get(droot, SLOT1) or {}).get("session_id") == "dispatch:001-mac-77"
    leases.release(droot, SLOT1, "dispatch:001-mac-77")
    fake_claude.scenario([one(envelope())])
    assert _new_fast(droot)["state"] == "done"


def test_a_live_marked_k1_repo_lease_refuses_new_and_resume(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("question"))])
    d = _new_fast(droot)
    leases.take_marked(droot, "repo:alpha", session_id="dispatch:001-mac-77", pid=os.getpid())
    for attempt in (
        lambda: _new_fast(droot),
        lambda: dispatch.resume(droot, dispatch_id=d["id"], answer="x"),
    ):
        with pytest.raises(
            AgentError, match="repo:alpha is held by dispatch:001-mac-77, a dispatch that runs without a repo"
        ):
            attempt()
    assert dispatch.load(droot, d["id"])["attempt"] == 1 and _held_by(droot, d["id"]) == []


def _tree(root: Path) -> list[tuple[str, bytes]]:
    base = root / ".agent"
    return sorted((str(p.relative_to(base)), p.read_bytes()) for p in base.rglob("*") if p.is_file())


def test_the_preview_reports_real_slot_availability_and_writes_only_the_order(droot: Path):
    """C2 allows the vault order and its binding, never a slot or dispatch record."""
    from agent_cli.dispatch import orders
    from agent_cli.ops.dispatch import dispatch_preview_text

    _set_alpha_dispatch(droot, max_concurrent=2)
    _foreign(droot, SLOT1)
    before = _tree(droot)
    p = _new_fast(droot, dry_run=True)
    # Plan §A3 (plan.md:60-64): a stable binding plus the legacy caller's latest pointer.
    manifests = [orders._path(droot, identity) for identity in (p["preview_id"], p["id"])]
    assert _tree(droot) == sorted(
        [*before, *((str(m.relative_to(droot / ".agent")), m.read_bytes()) for m in manifests)]
    )
    assert manifests[0].read_bytes() == manifests[1].read_bytes()
    assert (droot / p["work_order"]).read_bytes() == p["prompt"].encode("utf-8")
    assert p["repo_slots"]["cap"] == 2 and p["repo_slots"]["free"] == [0]
    assert p["repo_slots"]["held"] == [{"slot": 1, "session_id": "someone", "status": "live"}]
    text = dispatch_preview_text(p)
    assert "repo slots: 1 of 2 free; 1 by someone (live)" in text and "retired repo lease: free" in text
