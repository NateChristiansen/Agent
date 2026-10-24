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
from land_helpers import certified as certified  # fixture, re-exported for pytest
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


@pytest.mark.parametrize("status", ["proposed", "blocked"])
def test_a_read_only_escalation_keeps_its_scope_and_runs_on_an_inactive_feature(
    droot: Path, fake_claude, status: str
):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"])), one(envelope())])
    parent = _new_fast(droot, writes=[])
    assert parent["state"] == "question" and parent["writes"] == []
    _status(droot, status)
    child = _new_fast(droot, escalate_from=parent["id"], effort="high")
    assert child["state"] == "done" and child["writes"] == []
    assert state.load(droot, child["id"])["writes"] == []
    assert len(fake_claude.calls()) == 2


def test_an_implicit_scope_escalation_is_still_refused_before_allocation(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    parent = _new_fast(droot)
    _status(droot, "proposed")
    with pytest.raises(AgentError, match="is proposed; implementation needs it active"):
        _new_fast(droot, escalate_from=parent["id"], effort="high")
    assert [r["id"] for r in state.load_all(droot)] == [parent["id"]]


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
    def refuse(root: Path, cand: str, *, git: object, old: str, budget_raise: str | None) -> dict[str, Any]:
        raise AssertionError("a refused landing reached the certificate")

    monkeypatch.setattr(land, "_certificate", refuse)


def _nothing_landed(root: Path, main: str) -> None:
    assert git(root, "rev-parse", "main") == main
    assert git(root, "for-each-ref", land.REF_PREFIX) == ""
    assert not list((root / ".agent").glob("land/*.json"))


def _two_tasks(root: Path) -> None:
    f = features.resolve(root, "1")
    f.body = "## Tasks\n\n- [ ] build\n- [ ] build more\n"
    f.save()


def _finished(
    root: Path, ident: str, *, at: str = "feature", task: int = 1, sha: str | None = None, **extra: Any
) -> dict[str, Any]:
    """A done dispatch on `feature` whose committed attempt ended at `at`, as its terminal commit
    saves it."""
    sha = sha or git(root, "rev-parse", at)
    d = {
        **next((r for r in state.load_all(root) if r["id"] == ident), {}),
        "id": ident,
        "repo": "Harness",
        "short": "harness",
        "branch": "feature",
        "feature": 1,
        "kind": "work",
        "attempt": 1,
        "state": "done",
        "tasks": [task],
        "git": {"commits": [{"sha": sha, "subject": "work"}], "files": []},
        **extra,
    }
    if reconcile.RESULT_HEAD not in extra:
        d[reconcile.RESULT_HEAD] = {"attempt": d["attempt"], "sha": sha}
    return state.save(root, d)


def _work(root: Path, name: str, *, reset: str | None = None) -> str:
    """Commit `<name>.txt` on `feature` from a worktree beside the checkout, first resetting the
    branch to `reset` when given. Returns the new commit."""
    wt = root.parent / "feature-wt"
    if not wt.exists():
        git(root, "worktree", "add", "-q", str(wt), "feature")
    if reset:
        git(wt, "reset", "-q", "--hard", reset)
    (wt / f"{name}.txt").write_text(f"{name}\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", name)
    return git(wt, "rev-parse", "HEAD")


def _owed(root: Path) -> list[tuple[str, str]]:
    return sorted((i.source, i.item) for i in reconcile.ledger(root, "1").owed)


def _on_main(root: Path, path: str) -> bool:
    return bool(git(root, "ls-tree", "main", path))


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


@pytest.mark.parametrize("days", [1, 14])
def test_a_landing_whose_sources_cannot_be_worked_out_lands_nothing_and_owes_nothing(
    harness: Path, monkeypatch: pytest.MonkeyPatch, days: int
):
    from datetime import UTC, datetime, timedelta

    from agent_cli import land_followups, land_recover

    _uncertified(monkeypatch)
    f = features.resolve(harness, "1")
    f.body = "## Tasks\n\n- [ ] build\n- [ ] build more\n"
    f.save()
    landed = {"repo": "Harness", "short": "harness", "branch": "feature", "feature": 1, "attempt": 1}
    state.save(harness, {**landed, "id": "001-mac-01", "state": "done", "tasks": [1]})
    real = land_followups.landing_intents
    calls: list[str] = []

    def unreadable(root: Path, journal: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(str(journal["id"]))
        if len(calls) == 1:
            raise OSError("feature file unreadable")
        return real(root, journal)

    monkeypatch.setattr(land_followups, "landing_intents", unreadable)
    main = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="ledger sources"):
        land.land(harness, "feature", strategy="ff")
    _nothing_landed(harness, main)
    state.save(harness, {**landed, "id": "001-mac-02", "state": "running", "tasks": [2]})
    monkeypatch.setenv("AGENT_NOW", (datetime.now(UTC) + timedelta(days=days)).isoformat())
    land_recover.recover_all(harness, harness)
    assert reconcile.ledger(harness, "1").owed == []


def test_work_finished_on_the_branch_while_the_certificate_runs_is_not_accepted(
    harness: Path, monkeypatch: pytest.MonkeyPatch
):
    _two_tasks(harness)
    _finished(harness, "001-mac-01")

    def meanwhile(root: Path, cand: str, **_: object) -> dict[str, Any]:
        _finished(harness, "001-mac-02", task=2)
        return {"run_id": "certified-in-test", "reused": True}

    monkeypatch.setattr(land, "_certificate", meanwhile)
    land.land(harness, "feature", strategy="ff")
    assert _owed(harness) == [("accept:001-mac-01", "task#1")]


@pytest.mark.parametrize(
    "other",
    [
        {"state": "queued"},
        {"state": "running"},
        {"state": "question"},
        {"state": "cancelled"},
        {"kind": "probe"},
        {"writes": []},
        {"no_diff": {"attempt": 1}},
        {reconcile.RESULT_HEAD: None},  # finalized by code that kept no head: history
        {"attempt": 2},  # its head is the first attempt's; the second has not committed
    ],
)
def test_only_a_committed_result_with_work_on_the_branch_is_accepted_by_the_landing(
    harness: Path, certified, other: dict[str, Any]
):
    _two_tasks(harness)
    first = _finished(harness, "001-mac-01")
    observed_sha = first[reconcile.RESULT_HEAD]["sha"]
    head = {reconcile.RESULT_HEAD: {"attempt": 1, "sha": observed_sha}}
    _finished(harness, "001-mac-02", task=2, sha=observed_sha, **{**head, **other})
    land.land(harness, "feature", strategy="ff")
    assert _owed(harness) == [("accept:001-mac-01", "task#1")]


def test_a_result_finished_after_the_tips_were_pinned_is_not_accepted(
    harness: Path, certified, monkeypatch: pytest.MonkeyPatch
):
    from datetime import UTC, datetime

    _two_tasks(harness)
    _finished(harness, "001-mac-01")
    pin = land.require_members  # runs once the member tips are captured

    def late(root: Path, branches: list[str]) -> None:
        pin(root, branches)
        now = datetime.now(UTC).replace(microsecond=0).isoformat()  # the landing's own second
        _finished(harness, "001-mac-02", at=_work(harness, "late"), task=2, finished=now)

    monkeypatch.setattr(land, "require_members", late)
    land.land(harness, "feature", strategy="ff")
    assert not _on_main(harness, "late.txt")
    assert _owed(harness) == [("accept:001-mac-01", "task#1")]


def test_a_result_whose_work_the_branch_no_longer_holds_is_not_accepted(harness: Path, certified):
    _two_tasks(harness)
    _finished(harness, "001-mac-01")  # its work: a.txt, new.txt and gone.txt
    _finished(harness, "001-mac-02", at=_work(harness, "replacement", reset="main"), task=2)
    land.land(harness, "feature", strategy="ff")
    assert _on_main(harness, "replacement.txt") and not _on_main(harness, "new.txt")
    assert _owed(harness) == [("accept:001-mac-02", "task#2")]


def test_a_result_finished_between_the_landing_id_and_the_tip_capture_is_accepted(
    harness: Path, certified, monkeypatch: pytest.MonkeyPatch
):
    from datetime import UTC, datetime, timedelta

    _two_tasks(harness)
    capture = land.main_ref  # first runs after the landing's id is made, before any tip is read

    def finishing(git_obs: Any) -> str:
        if not state.load_all(harness):
            later = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
            _finished(harness, "001-mac-01", at=_work(harness, "included"), finished=later)
        return capture(git_obs)

    monkeypatch.setattr(land, "main_ref", finishing)
    land.land(harness, "feature", strategy="ff")
    assert _on_main(harness, "included.txt")
    assert _owed(harness) == [("accept:001-mac-01", "task#1")]


def test_each_result_generation_is_accepted_by_the_first_landing_that_brings_it(harness: Path, certified):
    def settle(source: str, task: int) -> None:
        item = {"item": f"task#{task}", "disposition": "not-accepted", "reason": "fixture, read"}
        reconcile.decide(harness, "1", source, [item])

    _two_tasks(harness)
    _finished(harness, "001-mac-01")
    _finished(harness, "001-mac-02", task=2)
    land.land(harness, "feature", strategy="merge")
    assert _owed(harness) == [("accept:001-mac-01", "task#1"), ("accept:001-mac-02", "task#2")]
    settle("accept:001-mac-01", 1)
    settle("accept:001-mac-02", 2)
    # The first dispatch resumed and committed a new result: a new generation, owed once more.
    # The second's unchanged result is still on the branch and is not owed again.
    _finished(harness, "001-mac-01", at=_work(harness, "third"), attempt=2, result={"summary": "again"})
    journal = land.land(harness, "feature", strategy="merge")
    assert _owed(harness) == [("accept:001-mac-01", "task#1")]
    [item] = reconcile.ledger(harness, "1").owed
    assert item.G.startswith("2:") and item.G.endswith("@" + journal["id"])


@pytest.mark.parametrize("days", [1, 14])
def test_a_stop_after_done_registers_only_the_landed_work_at_any_age(
    harness: Path, certified, monkeypatch: pytest.MonkeyPatch, days: int
):
    from datetime import UTC, datetime, timedelta

    from agent_cli import land_followups, land_recover, log

    _two_tasks(harness)
    _finished(harness, "001-mac-01")
    with monkeypatch.context() as stopped:
        stopped.setattr(land_followups, "reconcile", lambda *a: {})
        land.land(harness, "feature", strategy="ff")
    # Finished on the same branch after the landing; none of its work is on main.
    _finished(harness, "001-mac-02", at=_work(harness, "after"), task=2)
    monkeypatch.setenv("AGENT_NOW", (datetime.now(UTC) + timedelta(days=days)).isoformat())
    land_recover.recover_all(harness, harness)
    owed = sorted((i.source, i.item) for i in reconcile.ledger(harness, "1").owed)
    assert owed == [("accept:001-mac-01", "task#1")]
    land_recover.recover_all(harness, harness)
    entries = [reconcile.parse_entry(e) for e in log.parse(features.resolve(harness, "1").log_path)]
    assert [r.data["source"] for r in entries if isinstance(r, reconcile.Rec)] == ["accept:001-mac-01"]


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
