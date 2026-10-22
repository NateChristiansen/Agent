"""Cleanup failures cannot be accepted as successful work, and worktree cleanup, reconcile,
restore and scratch discard act on one dispatch's path against disposable Git fixtures."""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path

import pytest

from agent_cli import cli, dispatch, land_followups, links, ops, tidy, worktree
from agent_cli.core import AgentError, Output
from agent_cli.dispatch import worktrees
from agent_cli.mcp import specs_dispatch
from agent_cli.mcp.tools import Ctx
from conftest import git
from dispatch_helpers import (  # noqa: F401 — pytest fixture
    _new,
    _new_fast,
    droot,
    envelope,
    one,
    settle,
    worker_return,
)

MAIN = Path("repos/alpha-api")
CLI = importlib.import_module("agent_cli.cli.dispatch").HANDLERS  # `cli.dispatch` names a function


def _set(root: Path, ident: str, **fields) -> dict:
    d = dispatch.load(root, ident)
    d.update(fields)
    dispatch.save(root, d)
    return d


def _land(root: Path, ident: str) -> str:
    tip = git(["rev-parse", "HEAD"], Path(dispatch.load(root, ident)["worktree"])).strip()
    _set(root, ident, integration={"state": "landed", "tip": tip})
    return tip


def _lose(path: Path) -> None:
    """What the incident did: the directory vanished outside every safe route."""
    worktree.unlink_links(path)
    shutil.rmtree(path)


def _two(droot: Path, fake_claude) -> tuple[dict, dict]:
    fake_claude.scenario([one(envelope(), commit="T-42 one"), one(envelope(), commit="T-42 two")])
    d1 = _new_fast(droot)
    settle(droot, d1)
    d2 = _new_fast(droot)
    assert d1["worktree"] != d2["worktree"]
    return d1, d2


def _ctx(root: Path) -> Ctx:
    return Ctx(root=root, profile="manager")


def test_one_dispatch_cleanup_ignores_a_running_sibling_and_deletes_only_its_landed_branch(
    droot, fake_claude
):
    d1, d2 = _two(droot, fake_claude)
    _land(droot, d1["id"])
    _set(droot, d2["id"], state="running")
    with pytest.raises(AgentError, match="still running"):
        dispatch.cleanup(droot, feature="1")
    preview = dispatch.cleanup(droot, dispatch_id=d1["id"], dry_run=True)
    assert preview["would_remove"] == [d1["worktree"]] and Path(d1["worktree"]).is_dir()
    assert dispatch.load(droot, d1["id"]).get("worktree_removed") is False
    result = ops.dispatch_cleanup(droot, dispatch_id=d1["id"], delete_branches=True).data
    assert result["removed"] == [d1["worktree"]] and result["branches_deleted"] == [d1["branch"]]
    assert Path(d2["worktree"]).is_dir() and dispatch.load(droot, d2["id"])["state"] == "running"
    branches = git(["branch", "--list"], droot / MAIN)
    assert d1["branch"] not in branches and d2["branch"] in branches
    again = ops.dispatch_cleanup(droot, dispatch_id=d1["id"], delete_branches=True).data
    assert again["removed"] == [] and again["missing"] == [] and again["branches_deleted"] == []


def test_an_unlanded_branch_is_never_deleted(droot, fake_claude):
    d1, _ = _two(droot, fake_claude)
    result = specs_dispatch.dispatch(
        _ctx(droot), action="cleanup", dispatch_id=d1["id"], force=True, delete_branches=True
    ).data
    assert result["removed"] == [d1["worktree"]] and result["branches_deleted"] == []
    assert "unresolved tip" in result["branches_skipped"][0]["reason"]
    assert d1["branch"] in git(["branch", "--list"], droot / MAIN)


def test_blocked_unfinished_work_holds_even_when_its_tip_landed(droot, fake_claude):
    d1, _ = _two(droot, fake_claude)
    _land(droot, d1["id"])
    _set(droot, d1["id"], state="blocked")
    result = ops.dispatch_cleanup(droot, dispatch_id=d1["id"], delete_branches=True).data
    assert result["removed"] == [] and result["skipped"][0]["reason"] == "waiting on the owner"
    assert result["branches_deleted"] == []
    assert Path(d1["worktree"]).is_dir()


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_real_edits_hold_a_landed_tree(droot, fake_claude, kind):
    d1, _ = _two(droot, fake_claude)
    _land(droot, d1["id"])
    wt = Path(d1["worktree"])
    target = next(p for p in sorted(wt.iterdir()) if p.is_file() and p.name != ".git")
    if kind == "untracked":
        (wt / "notes.txt").write_text("worker output\n")
    else:
        target.write_text("edited\n")
        if kind == "staged":
            git(["add", target.name], wt)
    reason = dispatch.cleanup(droot, dispatch_id=d1["id"])["skipped"][0]["reason"]
    assert ("uncollected files" if kind == "untracked" else "dirty") in reason
    assert wt.is_dir()


def test_a_commit_during_cleanup_is_rechecked_under_the_lock(droot, fake_claude, monkeypatch):
    d1, _ = _two(droot, fake_claude)
    _land(droot, d1["id"])
    wt = Path(d1["worktree"])
    real = tidy.resolved

    def racing(*args, **kwargs):
        verdict = real(*args, **kwargs)
        if not (wt / "late.txt").exists():
            (wt / "late.txt").write_text("late\n")
            git(["add", "late.txt"], wt)
            git(["commit", "-m", "late work"], wt)
        return verdict

    monkeypatch.setattr(tidy, "resolved", racing)
    result = dispatch.cleanup(droot, dispatch_id=d1["id"])
    assert result["removed"] == [] and "changed while cleanup checked it" in result["skipped"][0]["reason"]
    assert wt.is_dir()


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_edits_made_after_the_first_check_are_rechecked_under_the_lock(droot, fake_claude, monkeypatch, kind):
    d1, _ = _two(droot, fake_claude)
    _land(droot, d1["id"])
    wt = Path(d1["worktree"])
    target = next(p for p in sorted(wt.iterdir()) if p.is_file() and p.name != ".git")
    real = tidy.resolved

    def editing(*args, **kwargs):
        if kind == "untracked":
            (wt / "late-output.txt").write_text("late\n")
        else:
            target.write_text("late edit\n")
            if kind == "staged":
                git(["add", target.name], wt)
        return real(*args, **kwargs)

    monkeypatch.setattr(tidy, "resolved", editing)
    result = dispatch.cleanup(droot, dispatch_id=d1["id"])
    assert result["removed"] == [] and wt.is_dir()
    reason = result["skipped"][0]["reason"]
    assert ("uncollected files" if kind == "untracked" else "uncommitted changes") in reason


def test_a_record_change_after_the_first_check_is_rechecked_under_the_lock(droot, fake_claude, monkeypatch):
    d1, _ = _two(droot, fake_claude)
    _land(droot, d1["id"])
    real, calls = tidy.active_holds, []

    def blocking(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:  # the recheck under both guards, after its records were read
            _set(droot, d1["id"], state="blocked")
        return real(*args, **kwargs)

    monkeypatch.setattr(tidy, "active_holds", blocking)
    result = dispatch.cleanup(droot, dispatch_id=d1["id"], force=True)
    assert len(calls) == 3 and result["removed"] == [] and Path(d1["worktree"]).is_dir()
    assert result["skipped"][0]["reason"] == worktrees.RECORD_CHANGED
    assert dispatch.load(droot, d1["id"])["worktree_removed"] is False


def _state_link(wt: Path, outside: Path) -> None:
    outside.mkdir()
    (outside / "sentinel.txt").write_text("keep\n")
    links.make(wt / "state-link", outside, is_dir=True)
    exclude = Path(git(["rev-parse", "--git-path", "info/exclude"], wt).strip())
    exclude = exclude if exclude.is_absolute() else wt / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("state-link\n")


def test_a_locked_registration_holds_before_any_link_is_unlinked(droot, fake_claude, tmp_path):
    d1, _ = _two(droot, fake_claude)
    _land(droot, d1["id"])
    wt = Path(d1["worktree"])
    _state_link(wt, tmp_path / "outside-state")
    git(["worktree", "lock", str(wt)], droot / MAIN)
    result = dispatch.cleanup(droot, dispatch_id=d1["id"], force=True)
    assert result["removed"] == [] and "locked" in result["skipped"][0]["reason"]
    assert links.is_link(wt / "state-link") and (tmp_path / "outside-state" / "sentinel.txt").exists()


def test_a_link_inside_the_tree_is_unlinked_and_its_external_target_survives(droot, fake_claude, tmp_path):
    d1, _ = _two(droot, fake_claude)
    _land(droot, d1["id"])
    wt = Path(d1["worktree"])
    _state_link(wt, tmp_path / "outside-state")
    assert dispatch.cleanup(droot, dispatch_id=d1["id"])["removed"] == [str(wt)]
    assert not wt.exists() and (tmp_path / "outside-state" / "sentinel.txt").read_text() == "keep\n"


def test_a_shared_path_marks_every_record_from_either_dispatch(droot, fake_claude):
    fake_claude.scenario([one(envelope())])
    d1 = _new_fast(droot)
    settle(droot, d1)
    d2 = _new_fast(droot)
    assert d1["worktree"] == d2["worktree"]
    result = dispatch.cleanup(droot, dispatch_id=d1["id"])
    assert result["removed"] == [d1["worktree"]]
    assert all(dispatch.load(droot, i)["worktree_removed"] for i in (d1["id"], d2["id"]))


def test_feature_cleanup_reports_a_blocked_group_and_goes_on(droot, fake_claude):
    d1, d2 = _two(droot, fake_claude)
    _land(droot, d2["id"])
    _set(droot, d1["id"], worker_cleanup_pending=True)
    for force in (False, True):
        preview = dispatch.cleanup(droot, feature="1", force=force, dry_run=True)
        assert preview["would_remove"] == [d2["worktree"]]
        assert preview["blocked"][0]["id"] == d1["id"] and "unconfirmed" in preview["blocked"][0]["reason"]
    result = dispatch.cleanup(droot, feature="1", force=True)
    assert result["removed"] == [d2["worktree"]] and Path(d1["worktree"]).is_dir()
    with pytest.raises(AgentError, match="cleanup is unconfirmed"):
        dispatch.cleanup(droot, feature="1", force=True)  # every remaining tree is blocked
    assert Path(d1["worktree"]).is_dir()


def test_an_abandoned_workers_scratch_is_discarded_only_by_its_confirmed_preview(droot, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    wt = Path(d["worktree"])
    (wt / "scratch.txt").write_text("worker scratch\n")
    (wt / "keep.txt").write_text("other\n")
    ctx = _ctx(droot)
    with pytest.raises(AgentError, match="only an abandoned worker"):
        specs_dispatch.dispatch(ctx, action="discard-files", dispatch_id=d["id"], paths=["scratch.txt"])
    _set(droot, d["id"], state="abandoned")
    assert "uncollected files" in dispatch.cleanup(droot, feature="1", force=True)["skipped"][0]["reason"]
    with pytest.raises(AgentError, match="needs `paths`"):
        specs_dispatch.dispatch(ctx, action="discard-files", dispatch_id=d["id"])
    preview = specs_dispatch.dispatch(ctx, action="discard-files", dispatch_id=d["id"], paths=["scratch.txt"])
    assert preview.data["dry_run"] and (wt / "scratch.txt").exists() and "owner agrees" in preview.text
    hashes = preview.data["plan_hashes"]
    with pytest.raises(AgentError, match="plan_hashes"):
        ops.dispatch_discard_files(droot, dispatch_id=d["id"], paths=["scratch.txt"], apply=True)
    (wt / "scratch.txt").write_text("changed since the preview\n")
    with pytest.raises(AgentError, match="plan_hashes"):
        ops.dispatch_discard_files(
            droot, dispatch_id=d["id"], paths=["scratch.txt"], plan_hashes=hashes, apply=True
        )
    assert (wt / "scratch.txt").exists()
    hashes = ops.dispatch_discard_files(droot, dispatch_id=d["id"], paths=["scratch.txt"]).data["plan_hashes"]
    confirmed = f"scratch.txt={hashes['scratch.txt']}"
    args = cli.parse(
        ["dispatch", "discard-files", d["id"], "--path", "scratch.txt", "--sha256", confirmed, "--apply"]
    )
    assert CLI[("dispatch", "discard-files")](droot, args, Output(as_json=True)) == 0
    assert not (wt / "scratch.txt").exists() and (wt / "keep.txt").exists()
    assert "scratch.txt" in dispatch.load(droot, d["id"])["discarded_files"]
    assert "keep.txt" in dispatch.cleanup(droot, feature="1", force=True)["skipped"][0]["reason"]


def test_a_missing_directory_is_reported_then_reconciled_separately_and_repeatably(
    droot, fake_claude, capsys
):
    d1, _ = _two(droot, fake_claude)
    wt, main = Path(d1["worktree"]), droot / MAIN
    _lose(wt)
    result = dispatch.cleanup(droot, dispatch_id=d1["id"])
    assert result["removed"] == [] and result["missing"][0]["state"] == "missing"
    assert dispatch.load(droot, d1["id"])["worktree_removed"] is False
    before = git(["worktree", "list", "--porcelain"], main)
    preview = specs_dispatch.dispatch(_ctx(droot), action="reconcile", dispatch_id=d1["id"])
    assert preview.data["dry_run"] and preview.data["holds"] == [] and not preview.data["reconciled"]
    assert preview.text.startswith("preview:")
    assert git(["worktree", "list", "--porcelain"], main) == before
    with pytest.raises(AgentError, match="needs `dispatch_id`"):
        specs_dispatch.dispatch(_ctx(droot), action="reconcile", apply=True)
    args = cli.parse(["--json", "dispatch", "reconcile", d1["id"], "--apply"])
    assert CLI[("dispatch", "reconcile")](droot, args, Output(as_json=True)) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["reconciled"] and applied["records_marked"] == [d1["id"]]
    assert str(wt).replace("\\", "/") not in git(["worktree", "list", "--porcelain"], main).replace("\\", "/")
    record = dispatch.load(droot, d1["id"])
    assert record["worktree_removed"] is True and record["worktree_lost"]["head"]
    assert record["state"] == "done"
    repeat = ops.dispatch_reconcile(droot, dispatch_id=d1["id"], apply=True)
    assert repeat.data["already"] and repeat.data["records_marked"] == []
    assert d1["branch"] in git(["branch", "--list"], main)


def test_an_unavailable_root_is_not_proof_of_deletion(droot, fake_claude, tmp_path):
    fake_claude.scenario([one(envelope())])
    d1 = _new_fast(droot)
    _set(droot, d1["id"], worktree=str(tmp_path / "unmounted" / "tree"))
    result = worktrees.reconcile(droot, dispatch_id=d1["id"], apply=True)
    assert result["state"] == "root-unavailable" and not result["reconciled"] and result["holds"]


def test_restore_puts_an_unfinished_checkout_back_at_its_recorded_head_only(droot, fake_claude):
    d1, _ = _two(droot, fake_claude)
    _set(droot, d1["id"], state="blocked")
    wt, main = Path(d1["worktree"]), droot / MAIN
    head = git(["rev-parse", "HEAD"], wt).strip()
    _lose(wt)
    wrong = specs_dispatch.dispatch(
        _ctx(droot), action="restore", dispatch_id=d1["id"], head="0" * 40, apply=True
    )
    assert not wrong.data["restored"] and not wt.exists()
    preview = ops.dispatch_restore(droot, dispatch_id=d1["id"])
    assert preview.data["head"] == head and not preview.data["restored"] and not wt.exists()
    assert worktree.LOST_FILES_WARNING in preview.data["warnings"] and "warning:" in preview.text
    done = ops.dispatch_restore(droot, dispatch_id=d1["id"], head=head, apply=True)
    assert done.data["restored"] and git(["rev-parse", "HEAD"], wt).strip() == head
    record = dispatch.load(droot, d1["id"])
    assert record["state"] == "blocked" and record["worktree_removed"] is False
    assert record["worktree_restored"]["head"] == head
    assert ops.dispatch_restore(droot, dispatch_id=d1["id"], head=head, apply=True).data["already"]
    _lose(wt)
    worktrees.reconcile(droot, dispatch_id=d1["id"], apply=True)
    assert dispatch.load(droot, d1["id"])["worktree_lost"]["head"] == head
    git(["update-ref", f"refs/heads/{d1['branch']}", f"{head}~1"], main)
    moved = worktrees.restore(droot, dispatch_id=d1["id"], apply=True)
    assert moved["head"] == head and not moved["restored"] and any("moved" in h for h in moved["holds"])
    assert not wt.exists()
    git(["update-ref", f"refs/heads/{d1['branch']}", head], main)
    wt.mkdir()
    (wt / "someone-else.txt").write_text("new occupant\n")
    occupied = worktrees.restore(droot, dispatch_id=d1["id"], apply=True)
    assert not occupied["restored"] and (wt / "someone-else.txt").exists()


@pytest.mark.parametrize("reconciled", [False, True])
def test_default_restore_is_bound_to_the_lost_head_not_the_branchs_current_tip(
    droot, fake_claude, reconciled
):
    d1, _ = _two(droot, fake_claude)
    _set(droot, d1["id"], state="blocked")
    wt, main = Path(d1["worktree"]), droot / MAIN
    lost_at = git(["rev-parse", "HEAD"], wt).strip()
    _lose(wt)
    if reconciled:
        worktrees.reconcile(droot, dispatch_id=d1["id"], apply=True)
    assert worktrees.restore(droot, dispatch_id=d1["id"])["head"] == lost_at
    child = git(["commit-tree", f"{lost_at}^{{tree}}", "-p", lost_at, "-m", "same-tree child"], main).strip()
    git(["update-ref", f"refs/heads/{d1['branch']}", child], main)
    moved = worktrees.restore(droot, dispatch_id=d1["id"], apply=True)
    assert moved["head"] == lost_at and not moved["restored"] and not wt.exists()
    assert any("moved" in h for h in moved["holds"])


def test_default_restore_needs_a_head_when_only_the_branch_records_it(droot, fake_claude):
    d1, _ = _two(droot, fake_claude)
    _set(droot, d1["id"], state="blocked")
    wt = Path(d1["worktree"])
    head = git(["rev-parse", "HEAD"], wt).strip()
    admin = Path(worktree.registration(droot / MAIN, wt)["admin"])
    _lose(wt)
    (admin / "logs" / "HEAD").unlink()
    assert worktree.registration(droot / MAIN, wt)["head_source"] == "ref"
    result = worktrees.restore(droot, dispatch_id=d1["id"], apply=True)
    assert not result["restored"] and any("pass the head" in h for h in result["holds"]) and not wt.exists()
    assert worktrees.restore(droot, dispatch_id=d1["id"], head=head, apply=True)["restored"]


def test_restore_refuses_finished_resolved_work(droot, fake_claude):
    d1, _ = _two(droot, fake_claude)
    head = _land(droot, d1["id"])
    _lose(Path(d1["worktree"]))
    result = worktrees.restore(droot, dispatch_id=d1["id"], head=head, apply=True)
    assert not result["restored"] and any("finished" in h for h in result["holds"])


def test_cleanup_output_is_bounded_and_the_cli_renders_it(droot, fake_claude, monkeypatch, capsys):
    rows = [{"id": f"001-mac-{i:02}", "worktree": f"/tmp/tree-{i}", "reason": "dirty"} for i in range(12)]
    full = {"feature": 1, "removed": [], "skipped": rows, "blocked": [], "missing": [], "branches_kept": []}
    monkeypatch.setattr(dispatch, "cleanup", lambda root, **kw: full)
    op = ops.dispatch_cleanup(droot, feature="1")
    assert len(op.data["skipped"]) == 10 and op.data["counts"]["skipped"] == 12
    assert json.loads(Path(op.data["evidence"]).read_text(encoding="utf-8"))["skipped"] == rows
    assert "skipped (12)" in op.text and ", 2 more" in op.text and "full result:" in op.text
    args = cli.parse(["dispatch", "cleanup", "1", "--delete-branches"])
    assert args.delete_branches is True
    assert cli.dispatch_cleanup(droot, args, Output()) == 0
    assert "/tmp/tree-0" in capsys.readouterr().out


def _landable(droot: Path, monkeypatch, fake_claude, count: int = 2) -> list[dict]:
    """`count` finished dispatches on alpha, which lands in-process as the harness repo would:
    its clone is the main checkout and any candidate counts as certified."""
    from types import SimpleNamespace

    from agent_cli import land

    fake_claude.scenario([one(envelope(), commit=f"T-42 work {i}") for i in range(count)])
    ds = []
    for _ in range(count):
        ds.append(_new_fast(droot))
        settle(droot, ds[-1])
    assert len({d["worktree"] for d in ds}) == count
    for i, d in enumerate(ds[1:], start=1):  # distinct files, so a batch merges cleanly
        wt = Path(d["worktree"])
        git(["mv", "worker.txt", f"worker-{i}.txt"], wt)
        git(["commit", "-q", "--amend", "--no-edit"], wt)
    name = ds[0]["repo"]
    monkeypatch.setattr("agent_cli.land.main_of", lambda root: droot / MAIN)
    monkeypatch.setattr("agent_cli.verify_record.harness_repo", lambda root: SimpleNamespace(name=name))
    monkeypatch.setattr(
        land, "_certificate", lambda root, cand, **_: {"run_id": "certified-in-test", "reused": True}
    )
    return ds


def _journal_of(droot: Path) -> dict:
    from agent_cli import land_journal as jn

    [found] = [j for j in jn.every(droot / MAIN) if j["state"] == "done"]
    return found


def test_a_landing_queues_cleanup_and_removes_the_tree_only_after_it_is_done(droot, fake_claude, monkeypatch):
    from agent_cli import land_journal as jn

    d1, d2 = _landable(droot, monkeypatch, fake_claude)
    main = droot / MAIN
    seen: list[tuple] = []
    advance, real = jn.advance, worktrees.cleanup

    def advancing(m, journal, state, **extra):
        seen.append(("advance", state, journal["obligations"]["cleanup"], Path(d1["worktree"]).is_dir()))
        return advance(m, journal, state, **extra)

    def cleaning(root, **kw):
        [journal] = [j for j in jn.every(main) if j.get("obligations")]
        seen.append(("cleanup", journal["state"], jn.lock_file(main).exists(), kw.get("dry_run", False)))
        return real(root, **kw)

    monkeypatch.setattr(jn, "advance", advancing)
    monkeypatch.setattr(worktrees, "cleanup", cleaning)
    op = ops.land(droot, branch=d1["branch"])
    # Queued before the transaction began, applied once the landing was done and its lock released.
    assert seen[0] == ("advance", "prepared", "pending", True)
    assert seen[-1] == ("cleanup", "done", False, False)
    assert [s for s in seen if s[0] == "cleanup"] == [seen[-1]]
    assert not Path(d1["worktree"]).exists() and Path(d2["worktree"]).is_dir()
    assert dispatch.load(droot, d1["id"])["worktree_removed"] is True
    journal = _journal_of(droot)
    assert journal["obligations"]["cleanup"] == "done"
    assert journal["cleanup"]["candidates"] == [
        {"id": d1["id"], "worktree": d1["worktree"], "branch": d1["branch"], "state": "removed", "holds": []}
    ]
    assert "cleanup: removed 1, held 0" in op.text and "retried" not in op.text
    calls = len(seen)
    land_followups.retry_cleanup(droot)  # a repeat finds nothing owed and removes nothing
    _new_fast(droot)  # nor does an allocation
    assert [s for s in seen[calls:] if s[0] == "cleanup"] == []


def test_a_held_candidate_is_reported_kept_and_removed_at_a_later_allocation(droot, fake_claude, monkeypatch):
    d1, d2 = _landable(droot, monkeypatch, fake_claude)
    wt = Path(d1["worktree"])
    (wt / "notes.txt").write_text("worker output\n")
    _set(droot, d1["id"], setup=[{"exit": 1}])  # never reused, so allocation may clean it
    op = ops.land(droot, branch=d1["branch"])
    assert op.data["state"] == "done" and wt.is_dir()
    [row] = land_followups.held(_journal_of(droot))
    assert row["id"] == d1["id"] and "uncollected files" in row["holds"][0]
    assert "held 1" in op.text and "uncollected files" in op.text and "retried at the next" in op.text
    status = ops.land(droot, status=True)
    assert status.data["landings"][0]["cleanup_held"] == [row] and "1 held" in status.text

    (wt / "notes.txt").unlink()
    before = {d["id"] for d in dispatch.state.load_all(droot)}
    real, records = worktrees.cleanup, []

    def cleaning(root, **kw):
        records.append({d["id"] for d in dispatch.state.load_all(droot)})
        return real(root, **kw)

    monkeypatch.setattr(worktrees, "cleanup", cleaning)
    _new_fast(droot)
    assert records == [before]  # retried before the new dispatch's record existed
    assert not wt.exists() and Path(d2["worktree"]).is_dir()
    assert _journal_of(droot)["obligations"]["cleanup"] == "done"


def test_a_failed_cleanup_leaves_the_landing_successful_and_is_retried(droot, fake_claude, monkeypatch):
    d1, _ = _landable(droot, monkeypatch, fake_claude)
    real = worktrees.cleanup

    def broken(root, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(worktrees, "cleanup", broken)
    op = ops.land(droot, branch=d1["branch"])
    journal = _journal_of(droot)
    assert op.data["state"] == "done" and journal["state"] == "done"
    assert git(["rev-parse", "main"], droot / MAIN).strip() == journal["cand"]
    assert "cleanup failed: disk on fire" in op.text and Path(d1["worktree"]).is_dir()
    assert journal["obligations"]["cleanup"] == "pending"
    monkeypatch.setattr(worktrees, "cleanup", real)
    land_followups.retry_cleanup(droot)
    assert not Path(d1["worktree"]).exists()
    assert _journal_of(droot)["obligations"]["cleanup"] == "done"


def test_a_landing_stopped_after_done_is_cleaned_at_the_next_opportunity(droot, fake_claude, monkeypatch):
    from agent_cli import land

    d1, _ = _landable(droot, monkeypatch, fake_claude)
    _set(droot, d1["id"], setup=[{"exit": 1}])  # never reused, so allocation may clean it
    real = land.followups.retry_cleanup
    monkeypatch.setattr(land.followups, "retry_cleanup", lambda root, **kw: None)  # stopped here
    ops.land(droot, branch=d1["branch"])
    assert Path(d1["worktree"]).is_dir() and _journal_of(droot)["obligations"]["cleanup"] == "pending"
    monkeypatch.setattr(land.followups, "retry_cleanup", real)
    _new_fast(droot)
    assert not Path(d1["worktree"]).exists()
    assert _journal_of(droot)["obligations"]["cleanup"] == "done"


def test_failed_aborted_previewed_and_legacy_landings_queue_no_cleanup(droot, fake_claude, monkeypatch):
    from agent_cli import land
    from agent_cli import land_journal as jn

    d1, _ = _landable(droot, monkeypatch, fake_claude)
    calls: list[dict] = []
    monkeypatch.setattr(worktrees, "cleanup", lambda root, **kw: calls.append(kw) or {})

    def refused(root, cand, **_):
        raise AgentError("the record failed")

    monkeypatch.setattr(land, "_certificate", refused)
    with pytest.raises(AgentError, match="the record failed"):
        ops.land(droot, branch=d1["branch"])
    _new_fast(droot, dry_run=True)
    main = droot / MAIN
    tip = git(["rev-parse", d1["branch"]], main).strip()
    base = {"branch": d1["branch"], "tip": tip, "cand": tip, "record": "r", "ts": jn.now()}
    aborted = {
        **base,
        "id": "20261020T010000-aaaaaa",
        "state": "aborted",
        "obligations": land_followups.initial(),
    }
    legacy = {
        **base,
        "id": "20261020T020000-bbbbbb",
        "state": "done",
        "obligations": {"milestone": "pending"},
    }
    for journal in (aborted, legacy):
        jn.write(main, journal)
    land_followups.retry_cleanup(droot)
    _new_fast(droot)
    assert calls == [] and Path(d1["worktree"]).is_dir()


def test_a_batch_cleans_each_member_and_leaves_other_work(droot, fake_claude, monkeypatch):
    d1, d2, d3 = _landable(droot, monkeypatch, fake_claude, count=3)
    op = ops.land(droot, branch=d1["branch"], extra=[d2["branch"]])
    assert op.data["state"] == "done" and "cleanup: removed 2, held 0" in op.text
    assert not Path(d1["worktree"]).exists() and not Path(d2["worktree"]).exists()
    assert Path(d3["worktree"]).is_dir()
    rows = _journal_of(droot)["cleanup"]["candidates"]
    assert {r["id"]: r["state"] for r in rows} == {d1["id"]: "removed", d2["id"]: "removed"}


def test_an_active_verifier_and_a_resumed_head_hold_until_their_exact_head_lands(
    droot, fake_claude, monkeypatch
):
    d1, d2 = _landable(droot, monkeypatch, fake_claude)
    _set(droot, d2["id"], worker_cleanup_pending=True)  # its verifier's cleanup is unconfirmed
    first = ops.land(droot, branch=d1["branch"], extra=[d2["branch"]])
    holds = {r["id"]: r["holds"] for r in land_followups.held(first.data)}
    assert list(holds) == [d2["id"]] and "unconfirmed" in holds[d2["id"]][0]
    assert Path(d2["worktree"]).is_dir() and not Path(d1["worktree"]).exists()

    _set(droot, d2["id"], worker_cleanup_pending=False, state="blocked")
    wt = Path(d2["worktree"])
    (wt / "resumed.txt").write_text("resumed work\n")
    git(["add", "resumed.txt"], wt)
    git(["commit", "-q", "-m", "resumed"], wt)
    _set(droot, d2["id"], state="done")
    land_followups.retry_cleanup(droot)  # the old landing's mark cannot remove the resumed head
    [row] = land_followups.held(_journal_of(droot))
    assert "kept until that exact head lands" in row["holds"][0] and wt.is_dir()

    second = ops.land(droot, branch=d2["branch"])
    assert not wt.exists() and "cleanup: removed 1" in second.text
    from agent_cli import land_journal as jn

    older = jn.read(droot / MAIN, first.data["id"])
    assert older["obligations"]["cleanup"] == "done"
    assert {r["id"]: r["state"] for r in older["cleanup"]["candidates"]}[d2["id"]] == "superseded"


@pytest.mark.parametrize("reason", ["completed", "budget_exhausted", "max_turns"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_harness_disconnect_failure_blocks_done(droot, fake_claude, monkeypatch, reason, cancelled):
    result = envelope(cost=0.42)
    result["terminal_reason"] = reason
    fake_claude.scenario([one(result)])

    def factory(options):
        transport = fake_claude.factory(options)
        close = transport.close

        async def failed_close():
            await close()
            if cancelled:
                record = dispatch.load(droot, "001-mac-01")
                record["state"] = "cancelled"
                dispatch.state.save(droot, record)
            raise RuntimeError("synthetic denied cleanup with a live descendant")

        transport.close = failed_close
        return transport

    monkeypatch.setattr(dispatch.supervisor, "transport_factory", factory)
    record = _new(droot, no_checks=True)
    assert record["state"] == ("cancelled" if cancelled else "failed")
    assert record["stop_reason"] == "cleanup_failed"
    assert record["exit_code"] == 1
    assert record["last_result"]["total_cost_usd"] == 0.42
    if not cancelled:
        assert record["cost_usd"] == 0.42
    # The worker's own status survives, stored once with the envelope pointing at it.
    assert worker_return(record)["status"] == "done"
    assert "structured_output" not in record["last_result"]
    if not cancelled:
        assert "cleanup failed" in record["error"]
    assert len(fake_claude.calls()) == 1
