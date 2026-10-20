"""A reverify run whose cleanup failed is finished later, once its verifier is provably done.

On Windows `lane.clear` once could not delete a temp root holding read-only git objects. The row
stayed `cleanup_pending`, which kept the lane and the worktree lease and refused every later
reverify and `dispatch cleanup`, because nothing ever called `lane.clear` for that run again.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import leases, links
from agent_cli.core import AgentError
from agent_cli.dispatch import holdings, lane, verification, worktrees
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load, save
from dispatch_helpers import droot  # noqa: F401
from test_dispatch_reverify import _blocked_by_dirt
from test_dispatch_verification import ready  # noqa: F401


def _temp(root: Path, row: dict) -> Path:
    return Path((root / row["run_area"] / "pytest-temproot").read_text().strip())


def _stuck(root: Path, d: dict, monkeypatch) -> dict:
    """A run whose `lane.clear` failed once."""
    _blocked_by_dirt(root, d)
    real = lane.clear
    calls: list[int] = []

    def once(root, binding):
        calls.append(1)
        return False if len(calls) == 1 else real(root, binding)

    monkeypatch.setattr(lane, "clear", once)
    row = reverify_mod.reverify(root, d["id"])
    assert row["cleanup_pending"] is True and row["result"]["status"] == "verified"
    assert _temp(root, row).exists()
    assert leases.get(root, "verify:alpha") is not None
    assert gate.requires_lease(root, load(root, d["id"]))
    return row


def _unchanged(root: Path, d: dict, row: dict) -> None:
    after = load(root, d["id"])
    assert after["reverify_runs"][-1]["cleanup_pending"] is True
    assert after["reverify_runs"][-1]["result"] == row["result"]
    assert leases.get(root, "verify:alpha") is not None
    assert gate.requires_lease(root, after)


def test_a_stuck_run_is_finished_by_the_next_reverify(ready, monkeypatch):
    root, d = ready
    row = _stuck(root, d, monkeypatch)
    temp = _temp(root, row)

    again = reverify_mod.reverify(root, d["id"])

    assert again["result"]["status"] == "verified" and again["cleanup_pending"] is False
    runs = load(root, d["id"])["reverify_runs"]
    assert [r["cleanup_pending"] for r in runs] == [False, False]
    assert runs[0]["result"] == row["result"] and runs[0]["cleanup_finished"]
    assert not temp.exists() and not Path(row["checkout"]).parent.exists()
    assert leases.get(root, "verify:alpha") is None
    assert (root / row["run_area"] / "terminal.json").exists()


def test_dispatch_cleanup_finishes_a_stuck_run(ready, monkeypatch):
    root, d = ready
    row = _stuck(root, d, monkeypatch)
    worktrees.cleanup(root, feature=str(d["feature"]), force=True)
    after = load(root, d["id"])
    assert after["reverify_runs"][0]["cleanup_pending"] is False
    assert not _temp(root, row).exists() and leases.get(root, "verify:alpha") is None
    assert after.get("worktree_removed") is True


def test_a_stuck_run_is_left_alone_until_finish_cleanup_can_free_the_lane_and_the_lease(
    ready, monkeypatch, tmp_path
):
    """Every refusal below leaves the row exactly as it was, so one stuck run serves them all,
    and the last step finishes it."""
    root, d = ready
    row = _stuck(root, d, monkeypatch)

    with pytest.raises(AgentError, match="cleanup is unconfirmed"):
        worktrees.cleanup(root, feature=str(d["feature"]), force=True, dry_run=True)
    _unchanged(root, d, row)
    assert _temp(root, row).exists()

    with monkeypatch.context() as m:
        m.setattr(
            verification,
            "load_status",
            lambda root, binding: SimpleNamespace(requires_lease=False, cleanup_confirmed=False),
        )
        assert reverify_mod.finish_cleanup(root, d["id"]) == []
        _unchanged(root, d, row)
    with monkeypatch.context() as m:
        m.setattr(
            verification,
            "load_status",
            lambda root, binding: SimpleNamespace(requires_lease=True, cleanup_confirmed=True),
        )
        assert reverify_mod.finish_cleanup(root, d["id"]) == []
        _unchanged(root, d, row)
        assert _temp(root, row).exists()
        with pytest.raises(AgentError, match="still active"):
            reverify_mod.reverify(root, d["id"])

    def broken(*_a, **_k):
        raise AgentError("simulated guard timeout")

    for target, name, value in (
        (lane, "clear", lambda root, binding: False),
        (reverify_mod, "shutil", SimpleNamespace(rmtree=lambda *_a, **_k: None)),
        (gate, "record_guard", broken),
    ):
        with monkeypatch.context() as m:
            m.setattr(target, name, value)
            assert reverify_mod.finish_cleanup(root, d["id"]) == [], name
            _unchanged(root, d, row)

    unrelated = tmp_path / "unrelated-project" / f"{d['id']}-reverify-x"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("not the harness's\n")
    for where in ("elsewhere", "link"):
        holder = unrelated
        if where == "link":
            holder = Path(tempfile.gettempdir()) / f"{d['id']}-reverify-link-{tmp_path.name}"
            links.make(holder, unrelated, is_dir=True)
        try:
            _point_checkout(root, d, holder / "tree")
            assert reverify_mod.finish_cleanup(root, d["id"]) == [], where
            assert (unrelated / "keep.txt").exists()
            _unchanged(root, d, row)
        finally:
            if holder != unrelated:
                os.unlink(holder) if holder.is_symlink() else os.rmdir(holder)
    _point_checkout(root, d, Path(row["checkout"]))

    assert reverify_mod.finish_cleanup(root, d["id"]) == [row["run_id"]]
    after = load(root, d["id"])
    assert after["reverify_runs"][0]["cleanup_pending"] is False
    assert not _temp(root, row).exists()
    assert leases.get(root, "verify:alpha") is None
    assert not gate.requires_lease(root, after)
    assert reverify_mod.finish_cleanup(root, d["id"]) == []


def test_a_checkout_that_cannot_be_removed_keeps_everything(ready, monkeypatch):
    root, d = ready
    _blocked_by_dirt(root, d)
    real = reverify_mod._git

    def no_remove(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            return 1, "simulated remove failure"
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(reverify_mod, "_git", no_remove)
    row = reverify_mod.reverify(root, d["id"])
    assert reverify_mod.finish_cleanup(root, d["id"]) == []
    _unchanged(root, d, row)
    assert Path(row["checkout"]).exists() and _temp(root, row).exists()
    monkeypatch.setattr(reverify_mod, "_git", real)
    assert reverify_mod.finish_cleanup(root, d["id"]) == [row["run_id"]]
    assert not Path(row["checkout"]).parent.exists() and not _temp(root, row).exists()
    assert leases.get(root, "verify:alpha") is None


def test_a_bindingless_row_only_needs_its_checkout_removed(ready, monkeypatch):
    root, d = ready
    _blocked_by_dirt(root, d)
    real = reverify_mod._git

    def boom(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            Path(args[3]).mkdir()
            raise TimeoutError("simulated capture timeout")
        if args[:2] == ("worktree", "remove"):
            return 1, "simulated remove failure"
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(reverify_mod, "_git", boom)
    with pytest.raises(TimeoutError):
        reverify_mod.reverify(root, d["id"])
    row = load(root, d["id"])["reverify_runs"][-1]
    assert row["binding"] is None and row["cleanup_pending"] is True
    monkeypatch.setattr(reverify_mod, "_git", real)
    shutil.rmtree(row["checkout"])  # git never registered it; a plain directory is what is left
    assert reverify_mod.finish_cleanup(root, d["id"]) == [row["run_id"]]
    after = load(root, d["id"])["reverify_runs"][-1]
    assert after["cleanup_pending"] is False and after["result"] == row["result"]
    assert leases.get(root, "verify:alpha") is None
    assert reverify_mod.reverify(root, d["id"])["result"]["status"] == "verified"


def _point_checkout(root: Path, d: dict, checkout: Path) -> None:
    with gate.record_guard(root, d["id"]) as current:
        current["reverify_runs"][-1]["checkout"] = str(checkout)
        save(root, current)


def test_a_lease_release_that_fails_is_retried(ready, monkeypatch):
    root, d = ready
    row = _stuck(root, d, monkeypatch)
    real = leases.release

    def broken(*_a, **_k):
        raise OSError("simulated lease file in use")

    monkeypatch.setattr(leases, "release", broken)
    assert reverify_mod.finish_cleanup(root, d["id"]) == []
    assert load(root, d["id"])["reverify_runs"][0]["cleanup_pending"] is False
    held = holdings.reverify_held(root, d["id"])[row["run_id"]]
    assert {area.split(":")[0] for area, _ in held} == {"verify", "worktree"}
    assert leases.get(root, "verify:alpha") is not None
    monkeypatch.setattr(leases, "release", real)
    again = reverify_mod.reverify(root, d["id"])
    assert again["result"]["status"] == "verified" and again["cleanup_pending"] is False
    assert holdings.reverify_held(root, d["id"]) == {}
    assert leases.get(root, "verify:alpha") is None


def test_dispatch_cleanup_frees_a_finished_runs_leftover_lane(ready, monkeypatch):
    root, d = ready
    row = _stuck(root, d, monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(holdings, "reverify_release", lambda *_a: None)
        assert reverify_mod.finish_cleanup(root, d["id"]) == []
        assert row["run_id"] in holdings.reverify_held(root, d["id"])
    worktrees.cleanup(root, feature=str(d["feature"]), force=True)
    assert holdings.reverify_held(root, d["id"]) == {}
    assert load(root, d["id"]).get("worktree_removed") is True


def test_a_run_still_in_flight_is_left_alone(ready, monkeypatch):
    root, d = ready
    _blocked_by_dirt(root, d)
    seen: list[list[str]] = []

    def prepare(_root, _checkout, **_kw):
        seen.append(reverify_mod.finish_cleanup(root, d["id"]))
        raise AgentError("stopped at preparation")

    monkeypatch.setattr(lane, "prepare", prepare)
    with pytest.raises(AgentError, match="stopped at preparation"):
        reverify_mod.reverify(root, d["id"])
    assert seen == [[]]
