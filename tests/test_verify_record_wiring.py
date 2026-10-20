"""Where pass records meet dispatch (ADR-0041): workers never reach the store, the no-diff
exemption names the record it relies on, and the supervisor reuses a record for a clean head
instead of running the verifier."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from agent_cli import verify_record
from agent_cli.dispatch import lane, no_diff, verification
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.codex_permissions import decision
from agent_cli.dispatch.rules import sdk_hooks
from agent_cli.dispatch.state import load
from agent_cli.hooks.worker_records import REASON, record_reach
from dispatch_helpers import droot  # noqa: F401 -- pytest fixture
from test_dispatch_verification import finish, ready  # noqa: F401 -- pytest fixtures
from test_verify_record import _git, harness  # noqa: F401 -- pytest fixture


@pytest.mark.parametrize(
    "command",
    [
        "cat .agent/verify/v1/abc/def.json",
        "cp x ~/harness/.agent/verify/v1/t/k.json",
        "ls .agent//verify",
        "python3 -c \"open('.agent/./verify/x','w')\"",
        "agent verify record HEAD",
        "bin/agent verify show main",
        "bin/agent --root /x verify record HEAD",
        "uv run python -m agent_cli verify record HEAD",
    ],
)
def test_a_worker_command_that_names_the_store_or_the_verb_is_refused(command: str):
    assert record_reach(command) == REASON


@pytest.mark.parametrize(
    "command", ["git diff --stat", "uv run pytest tests/test_verify_record.py", "grep -rn verify src"]
)
def test_other_commands_pass(command: str):
    assert record_reach(command) is None


def test_both_providers_refuse_a_worker_reaching_for_records(droot: Path, tmp_path: Path, monkeypatch):
    from agent_cli.dispatch import codex_permissions

    callback = sdk_hooks(tmp_path, tmp_path)["PreToolUse"][0].hooks[0]
    payload: Any = {"tool_name": "Bash", "tool_input": {"command": "agent verify record HEAD"}}
    result = cast(dict[str, Any], asyncio.run(callback(payload, None, {"signal": None})))
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == REASON
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(
        codex_permissions.state, "load", lambda *_args: {"repo": "alpha", "worktree": str(worktree)}
    )
    event = {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"open('.agent/verify/x','w')\""}}
    assert decision(droot, "eval", event) == ("deny", REASON)


def test_a_codex_worker_sandbox_cannot_write_the_store(harness: Path, tmp_path: Path):
    from agent_cli.dispatch.codex_transport import _writable_roots

    worktree = tmp_path / "wt"
    _git(harness, "worktree", "add", "-q", "--detach", str(worktree))
    store = verify_record.store(harness).resolve()
    roots = [Path(r).resolve() for r in _writable_roots(worktree)]
    assert not any(store == r or r in store.parents for r in roots)


def test_the_no_diff_exemption_names_the_record_it_relies_on(harness: Path, tmp_path: Path):
    start = _git(harness, "rev-parse", "HEAD")
    worktree = tmp_path / "wt"
    _git(harness, "worktree", "add", "-q", "--detach", str(worktree))
    assert no_diff.relied_on(harness, worktree, start) == no_diff.NO_RECORD
    made = verify_record.record(harness, start)
    assert no_diff.relied_on(harness, worktree, start) == verify_record.pointer(made)
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=product, check=True)
    assert no_diff.relied_on(harness, product, start) == no_diff.NO_RECORD


def test_a_clean_harness_head_with_a_record_is_reusable_and_a_dirty_one_is_not(harness: Path, tmp_path: Path):
    worktree = tmp_path / "wt"
    _git(harness, "worktree", "add", "-q", "--detach", str(worktree))
    assert verify_record.reusable(harness, worktree, None) is None
    made = verify_record.record(harness, "HEAD")
    head = _git(harness, "rev-parse", "HEAD")
    assert verify_record.reusable(harness, worktree, None) == (head, verify_record.pointer(made))
    (worktree / "tracked.txt").write_text("edited\n")
    assert verify_record.reusable(harness, worktree, None) is None


def _no_lane(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a reused record must not wait for a verification lane")

    monkeypatch.setattr(lane, "await_lane", refuse)
    monkeypatch.setattr(verification, "launch_prepared", refuse)


def _commit(wt: Path) -> str:
    (wt / "new.txt").write_text("x\n")
    _git(wt, "add", "new.txt")
    _git(wt, "commit", "-q", "-m", "change")
    return _git(wt, "rev-parse", "HEAD")


def test_the_supervisor_relies_on_an_applicable_record_and_acceptance_rechecks_it(ready, monkeypatch):
    root, d = ready
    wt = Path(d["worktree"])
    head = _commit(wt)
    pointer = f"full pass record {'a' * 32} (tree {'b' * 12})"
    record = {"run_id": "a" * 32, "tree": "b" * 40}
    monkeypatch.setattr(
        verify_record, "reusable", lambda root, worktree, base, selector=None: (head, pointer)
    )
    monkeypatch.setattr(verify_record, "lookup", lambda root, rev: (record, "ok"))
    _no_lane(monkeypatch)
    gate.execute(root, d["id"], d["attempt"])
    after = load(root, d["id"])
    assert after["verification"]["mode"] == "record"
    result = finish(root, after)["verification_result"]
    assert result == {"status": "verified", "reasons": [pointer]}


@pytest.mark.parametrize("change", ["dirty", "gone", "replaced"])
def test_a_reused_record_that_no_longer_holds_is_not_verified(ready, monkeypatch, change):
    root, d = ready
    wt = Path(d["worktree"])
    head = _commit(wt)
    pointer = f"full pass record {'a' * 32} (tree {'b' * 12})"
    monkeypatch.setattr(
        verify_record, "reusable", lambda root, worktree, base, selector=None: (head, pointer)
    )
    monkeypatch.setattr(
        verify_record, "lookup", lambda root, rev: ({"run_id": "a" * 32, "tree": "b" * 40}, "ok")
    )
    _no_lane(monkeypatch)
    gate.execute(root, d["id"], d["attempt"])
    if change == "dirty":
        (wt / "new.txt").write_text("edited\n")
    elif change == "gone":
        monkeypatch.setattr(verify_record, "lookup", lambda root, rev: (None, "no full pass record"))
    else:
        monkeypatch.setattr(
            verify_record, "lookup", lambda root, rev: ({"run_id": "c" * 32, "tree": "b" * 40}, "ok")
        )
    assert finish(root, load(root, d["id"]))["verification_result"]["status"] == "unverified"
