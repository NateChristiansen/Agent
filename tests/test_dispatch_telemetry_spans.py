"""Verification and reverify spans always close: an attempt whose verifier
was prepared records exactly one `dispatch.verification` event at its terminal commit, whatever the
terminal state, and every reverify run records one `dispatch.reverify` event once its id exists.

Uses `test_dispatch_telemetry`'s isolated sink and `test_dispatch_verification`'s controlled
verifier, through the real `finalize`, supervisor recovery, `cancel`, `collect` and `reverify`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, leases
from agent_cli.core import AgentError
from agent_cli.dispatch import recovery, terminal, verification
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load
from dispatch_helpers import _new, _set_alpha_dispatch, droot, envelope, one, settle  # noqa: F401
from test_dispatch_telemetry import _events, _queued, isolated_sink, troot, verifier_envs  # noqa: F401
from test_dispatch_verification import diagnose, finish, ready, running  # noqa: F401


def _span(root: Path, dispatch_id: str) -> dict[str, Any]:
    stash = load(root, dispatch_id)["verification_telemetry"]
    assert stash["event_id"]
    return stash


def _one_verification(root: Path, dispatch_id: str) -> dict[str, Any]:
    stash = _span(root, dispatch_id)
    events = _events(root, "dispatch.verification", dispatch_id)
    assert [e["event_id"] for e in events] == [stash["event_id"]]
    assert stash["recorded"] == stash["event_id"]
    assert events[0]["data"]["run_id"] == stash["run_id"]
    return events[0]


# ------------------------------------------------------------------ dispatch.verification


def test_exception_recovery_failed_records_the_prepared_span(
    troot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, verifier_envs
) -> None:
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    d = _queued(troot, monkeypatch)
    original = dispatch.supervisor.finalize
    calls: list[str] = []

    def crash_once(*args: Any) -> dict:
        calls.append("finalize")
        if len(calls) == 1:
            raise RuntimeError("supervisor crash after the verifier ran")
        return original(*args)

    monkeypatch.setattr(dispatch.supervisor, "finalize", crash_once)
    with pytest.raises(RuntimeError):
        dispatch.supervisor.run(troot, dispatch_id=d["id"])

    final = load(troot, d["id"])
    assert final["state"] == "failed" and final["terminal_committed"]
    event = _one_verification(troot, d["id"])
    assert event["outcome"] == "incomplete"
    assert verifier_envs[0]["AGENT_TELEMETRY_PARENT"] == event["event_id"]


def test_cancel_records_the_prepared_span_once(troot: Path, ready) -> None:
    with running(ready) as (root, d):
        cancelled = recovery.cancel(root, dispatch_id=d["id"])
        assert cancelled["state"] == "cancelled"
    event = _one_verification(root, d["id"])
    assert event["outcome"] != "verified"
    assert finish(root, d)["state"] == "cancelled"
    _one_verification(root, d["id"])


def test_lost_lease_retake_records_the_prepared_span_unverified(
    troot: Path, ready, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, d = ready
    gate.execute(root, d["id"], 1)

    def refuse(*args: Any, **kwargs: Any) -> dict:
        raise AgentError("held by a peer")

    monkeypatch.setattr(terminal.holdings.leases_mod, "take", refuse)
    blocked = finish(root, load(root, d["id"]))
    assert blocked["state"] == "blocked" and "lease changed" in blocked["error"]
    event = _one_verification(root, d["id"])
    assert event["outcome"] == "incomplete"


def test_cleanup_incomplete_then_collect_records_once_after_the_commit(troot: Path, ready) -> None:
    with running(ready) as (root, d):
        pending = finish(root, d)
        assert not pending.get("terminal_committed")
        assert _events(root, "dispatch.verification", d["id"]) == []
        assert "recorded" not in _span(root, d["id"])
    (collected,) = recovery.collect(root, dispatch_id=d["id"])
    assert collected["state"] == "done"
    assert _one_verification(root, d["id"])["outcome"] == "verified"
    recovery.collect(root, dispatch_id=d["id"])
    finish(root, load(root, d["id"]))
    _one_verification(root, d["id"])


def test_done_and_failed_check_paths_record_one_event_each(troot: Path, fake_claude) -> None:
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    done = _new(troot)
    outcome = _one_verification(troot, done["id"])["outcome"]
    assert outcome == "verified", f"outcome {outcome!r}\n{diagnose(troot, done['id'])}"
    settle(troot, done)

    _set_alpha_dispatch(troot, verify=[{"command": "false", "timeout_seconds": 3}], verify_timeout_seconds=5)
    failed = _new(troot)
    assert failed["state"] == "blocked"
    assessed = failed["verification_result"]["status"]
    assert assessed != "verified"
    assert _one_verification(troot, failed["id"])["outcome"] == assessed


# ------------------------------------------------------------------ dispatch.reverify


@pytest.fixture
def finished(troot: Path, fake_claude) -> dict:
    fake_claude.scenario([one(envelope())])
    d = _new(troot)
    assert d["state"] == "done"
    return d


def _break_git(monkeypatch: pytest.MonkeyPatch, verb: str) -> None:
    original = reverify_mod._git

    def broken(cwd: Path, *args: str, **kwargs: Any) -> tuple[int, str]:
        if args[:2] == ("worktree", verb):
            raise RuntimeError(f"git worktree {verb} broke")
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(reverify_mod, "_git", broken)


def _break_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(prepared: Any) -> Any:
        raise RuntimeError("launch broke")

    monkeypatch.setattr(verification, "launch_prepared", broken)


def _break_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    original = verification.wait

    def broken(handle: Any) -> Any:
        original(handle)
        raise RuntimeError("wait broke")

    monkeypatch.setattr(verification, "wait", broken)


BREAKS = {
    "checkout": lambda mp: _break_git(mp, "add"),
    "launch": _break_launch,
    "wait": _break_wait,
    "cleanup": lambda mp: _break_git(mp, "remove"),
}


@pytest.mark.parametrize("step", list(BREAKS))
def test_a_raising_reverify_still_records_its_event(
    troot: Path, finished: dict, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    BREAKS[step](monkeypatch)
    with pytest.raises(RuntimeError):
        reverify_mod.reverify(troot, finished["id"])
    (event,) = _events(troot, "dispatch.reverify", finished["id"])
    assert event["outcome"] == "incomplete"
    data = event["data"]
    if step == "checkout":
        assert data["run_id"] is None and data["worktree_s"] is None
    else:
        assert data["run_id"] and isinstance(data["worktree_s"], float)
    if step == "cleanup":
        assert isinstance(data["verify_s"], float)
    else:
        assert data["verify_s"] is None and event["duration_s"] is None


def test_a_second_reverify_gets_its_own_event(troot: Path, finished: dict, monkeypatch) -> None:
    with monkeypatch.context() as mp:
        BREAKS["checkout"](mp)
        with pytest.raises(RuntimeError):
            reverify_mod.reverify(troot, finished["id"])
    assert leases.get(troot, "repo-slot:alpha/0") is None
    row = reverify_mod.reverify(troot, finished["id"])

    events = _events(troot, "dispatch.reverify", finished["id"])
    assert [e["outcome"] for e in events] == ["incomplete", "verified"]
    assert len({e["event_id"] for e in events}) == 2
    assert events[1]["data"]["run_id"] == row["binding"]["run_id"]
