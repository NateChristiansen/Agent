"""Verification-only recovery for a terminal dispatch.

A worker committed good work, but the supervisor refused to verify the tree, and a
session-start writer left a generated file modified in the worker's worktree. `collect` never
reruns checks, `resume` starts model work again, and managers may not clean a worker worktree.
`reverify` reruns only the checks, on the recorded head, in a clean checkout it creates.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import leases
from agent_cli.core import AgentError
from agent_cli.dispatch import lane, test_select, verification
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load, save
from dispatch_helpers import _set_alpha_dispatch, droot  # noqa: F401
from test_dispatch_verification import finish, ready  # noqa: F401


def _blocked_by_dirt(root: Path, d: dict) -> dict:
    """Committed work verified once, then the worktree went dirty, so acceptance blocked it."""
    gate.execute(root, d["id"], 1)
    (Path(d["worktree"]) / "README.md").write_text("rewritten by a session-start writer\n")
    blocked = finish(root, load(root, d["id"]))
    assert blocked["state"] == "blocked" and blocked["terminal_committed"]
    return blocked


def _worktrees(path: str) -> str:
    return subprocess.run(
        ["git", "worktree", "list"], cwd=path, capture_output=True, text=True, check=True
    ).stdout


def test_rerun_verifies_the_recorded_head_in_a_clean_checkout_and_runs_no_repository_hook(ready):
    """`worktree add` would run a post-checkout hook from the repository; recovery runs none."""
    root, d = ready
    blocked = _blocked_by_dirt(root, d)
    recorded = gate.metadata(blocked)
    assert recorded is not None
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=d["worktree"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    sentinel = root / "post-checkout-ran"
    hook = Path(common) / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    hook.chmod(0o755)

    row = reverify_mod.reverify(root, d["id"])

    assert not sentinel.exists()
    assert row["result"]["status"] == "verified"
    assert row["observed_at"] == row["started"]
    assert row["head_sha"] == row["final_sha"]
    assert row["final_sha"] == recorded[0].final_sha
    assert row["binding"]["run_id"] != recorded[0].run_id
    assert row["worker_tree"]["observed"] and not row["worker_tree"]["tracked_clean"]
    assert row["worker_tree"]["verified"] is False
    after = load(root, d["id"])
    assert after["state"] == "blocked"  # the historical terminal record is not regenerated
    assert gate.metadata(after) == recorded  # the old binding and its evidence stay as they were
    assert len(after["reverify_runs"]) == 1 and not after["reverify_runs"][0]["cleanup_pending"]
    assert (Path(d["worktree"]) / "README.md").read_text() == "rewritten by a session-start writer\n"
    assert not Path(row["checkout"]).exists() and row["checkout"] not in _worktrees(d["worktree"])
    assert leases.get(root, "verify:alpha") is None


def test_a_failing_check_is_not_verified(ready):
    """The verifier stops at the first failing command, and the result says which one failed."""
    root, d = ready
    _blocked_by_dirt(root, d)
    _set_alpha_dispatch(root, verify=[{"command": "false", "timeout_seconds": 3}], verify_timeout_seconds=5)
    row = reverify_mod.reverify(root, d["id"])
    assert row["result"]["status"] == "failed"
    assert row["result"]["reasons"][0].startswith("`false` exited 1 (run ")
    assert leases.get(root, "verify:alpha") is None


def test_refusals_run_nothing_and_leave_no_lease(ready, monkeypatch):
    """Each refusal leaves the blocked record as it was, so one blocked dispatch serves them all;
    the moved head goes last because it commits."""
    root, d = ready
    with pytest.raises(AgentError, match="not terminal"):
        reverify_mod.reverify(root, d["id"])
    _blocked_by_dirt(root, d)

    def refused(match: str, edit=None) -> None:
        current = load(root, d["id"])
        kept = copy.deepcopy(current)
        if edit is not None:
            edit(current)
            save(root, current)
        with pytest.raises(AgentError, match=match):
            reverify_mod.reverify(root, d["id"])
        assert leases.get(root, "verify:alpha") is None
        save(root, {**kept, "record_revision": load(root, d["id"])["record_revision"]})

    def bad_sha(record: dict) -> None:
        record["verification"]["binding"]["final_sha"] = "0" * 40  # no longer matches its store

    refused("unreadable", lambda record: record.update(reverify_runs=["not a row"]))
    refused("unreadable", bad_sha)
    with monkeypatch.context() as m:
        m.setattr(verification, "load_status", lambda root, binding: SimpleNamespace(requires_lease=True))
        refused("still active")
    real = reverify_mod._git
    with monkeypatch.context() as m:
        m.setattr(
            reverify_mod,
            "_git",
            lambda cwd, *args, **kwargs: (1, "") if args[:1] == ("cat-file",) else real(cwd, *args, **kwargs),
        )
        refused("missing from the repository")
    with monkeypatch.context() as m:
        m.setattr(
            test_select,
            "select",
            lambda *_args: test_select.Selection("refused", "base predates selection v2", (), policy=2),
        )
        refused(
            "no scoped selection to rerun: base predates", lambda record: record.update(selection_policy=2)
        )
    assert "reverify_runs" not in load(root, d["id"])

    wt = d["worktree"]
    subprocess.run(["git", "add", "README.md"], cwd=wt, check=True)
    subprocess.run(
        ["git", "-c", "user.name=W", "-c", "user.email=w@example.invalid", "commit", "-qm", "later"],
        cwd=wt,
        check=True,
    )
    refused("HEAD differs")


def test_a_failed_checkout_cleanup_keeps_the_lease_and_refuses_another_run(ready, monkeypatch):
    root, d = ready
    _blocked_by_dirt(root, d)
    real = reverify_mod._git

    def no_remove(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            return 1, "simulated remove failure"
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(reverify_mod, "_git", no_remove)
    row = reverify_mod.reverify(root, d["id"])
    assert row["cleanup_pending"] is True
    after = load(root, d["id"])
    assert gate.requires_lease(root, after)
    assert leases.get(root, "verify:alpha") is not None
    with pytest.raises(AgentError, match="cleanup failed"):
        reverify_mod.reverify(root, d["id"])
    monkeypatch.setattr(reverify_mod, "_git", real)
    real(Path(d["worktree"]), "worktree", "remove", "--force", row["checkout"])


@pytest.mark.parametrize("partial", [False, True])
def test_a_checkout_that_raises_releases_the_lease_or_records_why_not(ready, monkeypatch, partial):
    """A capture timeout or OSError in `worktree add` must not strand the lease unexplained."""
    root, d = ready
    _blocked_by_dirt(root, d)
    real = reverify_mod._git
    seen: list[Path] = []

    def boom(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            checkout = Path(args[3])
            seen.append(checkout)
            if partial:
                checkout.mkdir()
            raise TimeoutError("simulated capture timeout")
        if args[:2] == ("worktree", "remove") and partial:
            return 1, "simulated remove failure"
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(reverify_mod, "_git", boom)
    with pytest.raises(TimeoutError):
        reverify_mod.reverify(root, d["id"])
    after = load(root, d["id"])
    if partial:
        assert leases.get(root, "verify:alpha") is not None and gate.requires_lease(root, after)
        assert after["reverify_runs"][-1]["binding"] is None
        assert after["reverify_runs"][-1]["cleanup_pending"] is True
    else:
        assert leases.get(root, "verify:alpha") is None
        assert not seen[0].parent.exists()
        assert not after.get("reverify_runs")


def _policy_2(root: Path, dispatch_id: str) -> None:
    d = load(root, dispatch_id)
    d["selection_policy"] = 2
    save(root, d)


def test_a_policy_2_rerun_runs_its_scoped_selection(ready, monkeypatch):
    """Codex's phase 3-5 review: recovery took the repo's commands whole, so a policy-2 dispatch
    reran the full suite (ADR-0045)."""
    root, d = ready
    _blocked_by_dirt(root, d)
    _policy_2(root, d["id"])
    _set_alpha_dispatch(root, verify=[{"command": "uv run pytest", "timeout_seconds": 30}])
    scoped = test_select.Selection("scoped", "selected", ("README.md",), ("tests/test_x.py",), policy=2)
    monkeypatch.setattr(test_select, "select", lambda *_args: scoped)
    asked = []

    def prepare(_root, _checkout, **kw):
        asked.append(kw["config"])
        raise AgentError("stopped at preparation")

    monkeypatch.setattr(lane, "prepare", prepare)
    with pytest.raises(AgentError, match="stopped at preparation"):
        reverify_mod.reverify(root, d["id"])
    assert [c.command for c in asked[0].commands] == ["uv run pytest tests/test_x.py"]
    assert [r["scope"] for r in load(root, d["id"])["verification_scopes"]][-1] == "scoped"


@pytest.mark.parametrize("observed_at", [None, "2026-10-01T00:00:00+00:00"])
def test_reverify_republish_keeps_original_observation(ready, monkeypatch, observed_at):
    root, d = ready
    _blocked_by_dirt(root, d)
    _policy_2(root, d["id"])
    scoped = test_select.Selection(
        "scoped", "selected", ("README.md",), ("tests/test_x.py",), policy=2, unmapped_paths=()
    )
    current = load(root, d["id"])
    original = scoped.record(1)
    if observed_at is not None:
        original.update(observed_at=observed_at, base_sha="old-base", head_sha="old-head")
    current["verification_scopes"] = [original]
    save(root, current)
    original = load(root, d["id"])["verification_scopes"][0]
    monkeypatch.setattr(test_select, "select", lambda *_args: scoped)
    row = reverify_mod.reverify(root, d["id"])
    after = load(root, d["id"])
    assert after["verification_scopes"] == [original]
    assert row["observed_at"] == row["started"]
    assert row["base_sha"] == after.get("start_sha")
    assert row["head_sha"] == row["final_sha"]
    assert row["unmapped_paths"] == []
