"""The activity gate at its boundaries: launch, escalation, resume, the supervisor's loop top and
automatic retry, and landing. Implementation needs an active feature that owes nothing; a refusal
allocates nothing, keeps the attempt number, and lands nothing. Read-only work always passes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, features, land, leases, reconcile
from agent_cli.core import AgentError
from agent_cli.dispatch import lifecycle, state, supervisor
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401
from land_helpers import git, owned
from land_helpers import harness as harness  # fixture, re-exported for pytest


@pytest.fixture(autouse=True)
def _manager(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests act as the manager; an inherited worker id would hide a refusal.
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)


def _status(root: Path, status: str) -> None:
    f = features.resolve(root, "1")
    f.meta["status"] = status
    f.save()


def _local(root: Path, body: str) -> None:
    (root / "agent.local.yaml").write_text(body, encoding="utf-8")


def _nothing_allocated(root: Path, fake_claude) -> None:
    assert state.load_all(root) == []
    assert fake_claude.calls() == []
    assert leases.get(root, "repo-slot:alpha/0") is None


# ------------------------------------------------------------------ launch and escalation


@pytest.mark.parametrize("status", ["proposed", "blocked"])
def test_an_inactive_feature_refuses_implementation_launch_but_previews_read_only(
    droot: Path, fake_claude, status: str
):
    assert _new_fast(droot, dry_run=True)["dry_run"] is True
    _status(droot, status)
    with pytest.raises(AgentError, match=f"is {status}; implementation needs it active"):
        _new_fast(droot, dry_run=True)
    with pytest.raises(AgentError, match=f"is {status}"):
        _new_fast(droot, writes=["README.md"])
    _nothing_allocated(droot, fake_claude)
    assert _new_fast(droot, dry_run=True, writes=[])["dry_run"] is True
    assert _new_fast(droot, dry_run=True, kind="probe")["dry_run"] is True


def test_a_read_only_launch_runs_on_a_proposed_feature(droot: Path, fake_claude):
    _status(droot, "proposed")
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot, writes=[])
    assert d["state"] == "done" and len(fake_claude.calls()) == 1


def test_owed_items_and_a_held_task_refuse_before_allocation(droot: Path, run, fake_claude):
    src = reconcile.add_manual(droot, "1", ["review finding"])["source"]
    with pytest.raises(AgentError, match="1 item\\(s\\) to reconcile"):
        _new_fast(droot)
    cap = reconcile.capture(droot, "1", source=src, item="review finding", text="fix the finding")
    with pytest.raises(AgentError, match="awaits approval"):
        _new_fast(droot, tasks=[str(cap["task"])])
    _nothing_allocated(droot, fake_claude)
    assert _new_fast(droot, dry_run=True, tasks=["1"])["dry_run"] is True
    reconcile.approve(droot, "1", of=cap["capture"], evidence="owner approved in chat")
    assert _new_fast(droot, dry_run=True, tasks=[str(cap["task"])])["dry_run"] is True


def test_parking_still_refuses_first(droot: Path, fake_claude):
    _status(droot, "proposed")
    _local(droot, "maintenance:\n  parked: ['001']\n")
    with pytest.raises(AgentError, match="is parked"):
        _new_fast(droot)
    _nothing_allocated(droot, fake_claude)


def test_escalation_is_refused_once_the_feature_is_inactive(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    d = _new_fast(droot)
    _status(droot, "blocked")
    with pytest.raises(AgentError, match="is blocked"):
        _new_fast(droot, escalate_from=d["id"], effort="high")
    assert [r["id"] for r in state.load_all(droot)] == [d["id"]]
    assert len(fake_claude.calls()) == 1


# ------------------------------------------------------------------ resume


def test_an_implementation_resume_is_refused_and_a_read_only_one_runs(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"])), one(envelope())])
    d = _new_fast(droot)
    _status(droot, "proposed")
    with pytest.raises(AgentError, match="is proposed"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="8080")
    after = dispatch.load(droot, d["id"])
    assert after["attempt"] == 1 and after["state"] == "question"
    resumed = dispatch.resume(droot, dispatch_id=d["id"], answer="report only", writes=[], watch=True)
    assert resumed["attempt"] == 2 and resumed["writes"] == []
    assert len(fake_claude.calls()) == 2


# ------------------------------------------------------------------ the supervisor


def test_a_queued_dispatch_whose_feature_turned_inactive_is_not_started(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    fake_claude.scenario([one(envelope())])

    def start(root: Path, d: dict[str, Any], *, watch: bool) -> dict[str, Any]:
        _status(root, "blocked")
        return supervisor.run(root, dispatch_id=d["id"])

    monkeypatch.setattr(lifecycle, "start", start)
    d = _new_fast(droot)
    assert d["state"] == "blocked" and d["attempt"] == 1
    assert "implementation attempt not started" in d["error"] and "is blocked" in d["error"]
    assert fake_claude.calls() == []


def _after_first_attempt(monkeypatch: pytest.MonkeyPatch, change) -> None:
    """Run `change` once the first attempt has finished, as a status change or a new source that
    arrived while its worker ran."""
    launch = supervisor._launch_attempt

    def wrapped(root: Path, d: dict[str, Any], feat, rp) -> dict[str, Any]:
        out = launch(root, d, feat, rp)
        if out["attempt"] == 1:
            change(root)
        return out

    monkeypatch.setattr(supervisor, "_launch_attempt", wrapped)


def test_a_running_attempt_finishes_and_its_retry_is_refused(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    from dispatch_helpers import _set_alpha_dispatch

    _set_alpha_dispatch(droot, timeout_minutes=0.01)
    fake_claude.scenario([one(envelope(), sleep=20), one(envelope())])
    _after_first_attempt(monkeypatch, lambda root: _status(root, "blocked"))
    d = _new_fast(droot)
    assert d["state"] == "blocked" and d["attempt"] == 1 and d["auto_resumes"] == []
    assert "implementation attempt not started" in d["error"]
    assert len(fake_claude.calls()) == 1


def _sibling(root: Path, attempt: int, summary: str) -> dict[str, Any]:
    """Another dispatch on the feature whose finished `attempt` registers a result source."""
    d: dict[str, Any] = {
        **(state.load(root, "001-zz-01") if attempt > 1 else {}),
        "id": "001-zz-01",
        "repo": "alpha",
        "short": "zz",
        "feature": 1,
        "kind": "work",
        "attempt": attempt,
        "state": "done",
        "tasks": [],
        "result": {"status": "done", "summary": summary},
    }
    d[reconcile.INTENT] = [reconcile.dispatch_source(d)]
    return state.save(root, d)


def test_a_new_generation_registered_during_the_attempt_refuses_the_retry(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    from dispatch_helpers import _set_alpha_dispatch

    _sibling(droot, 1, "first answer")
    reconcile.decide(
        droot, "1", "dispatch:001-zz-01", [{"item": "result", "disposition": "no-task", "reason": "ok"}]
    )
    _set_alpha_dispatch(droot, timeout_minutes=0.01)
    fake_claude.scenario([one(envelope(), sleep=20), one(envelope())])
    _after_first_attempt(monkeypatch, lambda root: _sibling(root, 2, "second answer"))
    d = _new_fast(droot)
    assert d["state"] == "blocked" and d["attempt"] == 1
    assert "dispatch:001-zz-01 result" in d["error"]
    assert len(fake_claude.calls()) == 1


# ------------------------------------------------------------------ landing


def _branch(root: Path, name: str) -> None:
    git(root, "switch", "-q", "-c", name, "main")
    (root / f"{name}.txt").write_text(f"{name}\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"{name} work")
    git(root, "switch", "-q", "main")


def _uncertified(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(root: Path, cand: str) -> dict[str, Any]:
        raise AssertionError("a refused landing reached the certificate")

    monkeypatch.setattr(land, "_certificate", refuse)


def _nothing_landed(root: Path, main: str) -> None:
    assert git(root, "rev-parse", "main") == main
    assert git(root, "for-each-ref", land.REF_PREFIX) == ""
    assert not list((root / ".agent").glob("land/*.json"))


def test_an_inactive_member_is_refused_before_any_candidate_or_certificate(
    harness: Path, monkeypatch: pytest.MonkeyPatch
):
    _uncertified(monkeypatch)
    owned(harness, status="proposed")
    main = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="feature: Feature 001 is proposed"):
        land.land(harness, "feature", strategy="squash")
    _nothing_landed(harness, main)


def test_one_bad_member_refuses_the_whole_batch_and_names_it(harness: Path, monkeypatch: pytest.MonkeyPatch):
    _uncertified(monkeypatch)
    _branch(harness, "x")
    _branch(harness, "stray")
    main = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="the batch lands only as a whole") as refused:
        land.land(harness, "x", extra=["stray"])
    assert "stray: no dispatch record or owning feature" in str(refused.value)
    assert "x:" not in str(refused.value)
    _nothing_landed(harness, main)


def test_owed_items_refuse_a_landing(harness: Path, monkeypatch: pytest.MonkeyPatch):
    _uncertified(monkeypatch)
    reconcile.add_manual(harness, "1", ["acceptance"])
    main = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="to reconcile"):
        land.land(harness, "feature", strategy="ff")
    _nothing_landed(harness, main)


def test_the_dispatch_record_decides_ownership_before_the_binding(
    harness: Path, monkeypatch: pytest.MonkeyPatch
):
    _uncertified(monkeypatch)
    state.save(
        harness,
        {"id": "007-mac-01", "repo": "Harness", "short": "harness", "branch": "feature", "feature": 7},
    )
    main = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="^land refused.*feature: "):
        land.land(harness, "feature", strategy="ff")
    _nothing_landed(harness, main)
