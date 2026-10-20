"""A landing's milestone and task notices are obligations recovery can settle (E5/E6/F).

Three tasks were once ticked in one commit long after the first landed. A landing is not acceptance, so nothing ticks: the
feature's managers are told, and STATUS marks the landed work. Each obligation is written into the
prepared land journal and settled after `done`, by the landing or by recovery, metadata only.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import land_followups as followups
from agent_cli import land_journal as jn
from agent_cli import land_recover, ops, peers, sessions, status
from agent_cli.dispatch import state
from agent_cli.journal import pending
from agent_cli.ops import peer_wake

BRANCH = "agent/001-x-mac-01"
DAY_TS = "2026-10-09T01:00:00-06:00"


@pytest.fixture
def landed(tmp_root: Path, notes_remote: Path, run, monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Feature 001 with two tasks, dispatch 001-mac-01 for task 1 launched by `mgr-a`, a live
    Claude manager. Returns the ring outcomes to hand out (None accepted, a string refused)."""
    outcomes: list[str | None] = []
    monkeypatch.setenv("AGENT_NOW", DAY_TS)
    monkeypatch.setattr("agent_cli.verify_record.harness_repo", lambda root: SimpleNamespace(name="Harness"))
    monkeypatch.setattr("agent_cli.land.main_of", lambda root: root)
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    # Windows derives no inbox from a pid, so the record's address is given outright.
    monkeypatch.setattr("agent_cli.ops.peers.inbox_address", lambda target: "mgr-a-inbox")
    monkeypatch.setattr(
        "agent_cli.ops.peers._ring_socket", lambda path: outcomes.pop(0) if outcomes else None
    )
    run(["feature", "new", "x", "--title", "X"])
    ops.task_add(tmp_root, text="build the thing", feature="1")
    ops.task_add(tmp_root, text="accept it natively", feature="1")
    sessions.set_record(tmp_root, "mgr-a", name="mgr-a", pid=os.getpid())
    state.save(
        tmp_root,
        {
            "id": "001-mac-01",
            "repo": "Harness",
            "branch": BRANCH,
            "state": "done",
            "feature": 1,
            "tasks": [1],
            "launched_by_session": "mgr-a",
        },
    )
    return outcomes


def _journal(ident: str = "20261009T010000-aaaaaa", **extra: object) -> dict:
    return {
        "id": ident,
        "record": "a" * 32,
        "cand": "c" * 40,
        "branch": BRANCH,
        "state": "done",
        "ts": DAY_TS,
        "obligations": followups.initial(),
        **extra,
    }


def _rows(root: Path) -> list[dict]:
    return peers.peek(root, "mgr-a")


def _landings(root: Path) -> list[dict]:
    from datetime import date

    return [i for i in pending.read(root, date(2026, 10, 9)) if i.get("kind") == "landing"]


def test_a_landing_with_an_unticked_task_tells_the_manager_and_records_its_milestone(tmp_root: Path, landed):
    journal = _journal()
    obligations = followups.reconcile(tmp_root, tmp_root, journal)
    assert obligations == {"milestone": "recorded", "notices": "done"}
    rows = _rows(tmp_root)
    assert (
        len(rows) == 1 and "task(s) #1 landed on main" in rows[0]["text"] and "001-mac-01" in rows[0]["text"]
    )
    assert [i["key"] for i in _landings(tmp_root)] == [journal["id"]]
    record = state.load(tmp_root, "001-mac-01")
    assert record["integration"]["state"] == "landed"
    assert record["integration"]["notices"]["mgr-a"]["rang"] == "accepted"
    assert status.render(tmp_root)["features"][0]["landed_unticked"] == [1]


def test_a_ticked_task_sends_nothing(tmp_root: Path, landed):
    ops.task_done(tmp_root, task="1", feature="1")
    assert followups.reconcile(tmp_root, tmp_root, _journal())["notices"] == "done"
    assert _rows(tmp_root) == []


def test_recovery_of_a_done_landing_delivers_a_failed_notice_with_no_git(
    tmp_root: Path, landed, monkeypatch: pytest.MonkeyPatch
):
    original = peers.send

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(peers, "send", broken)
    journal = _journal()
    jn.write(tmp_root, journal)
    followups.reconcile(tmp_root, tmp_root, journal)
    assert jn.read(tmp_root, journal["id"])["obligations"]["notices"] == "pending"
    assert state.load(tmp_root, "001-mac-01")["integration"]["state"] == "landed"

    monkeypatch.setattr(peers, "send", original)
    monkeypatch.setattr("agent_cli.land_observe.Git", lambda *a, **k: pytest.fail("recovery replayed Git"))
    land_recover.recover(tmp_root, journal["id"])
    assert jn.read(tmp_root, journal["id"])["obligations"] == {"milestone": "recorded", "notices": "done"}
    assert len(_rows(tmp_root)) == 1


def test_a_stop_right_after_done_is_settled_by_recovery(tmp_root: Path, landed):
    journal = _journal()
    jn.write(tmp_root, journal)  # done on disk, nothing reconciled yet
    land_recover.recover(tmp_root, journal["id"])
    assert jn.read(tmp_root, journal["id"])["obligations"] == {"milestone": "recorded", "notices": "done"}
    assert len(_rows(tmp_root)) == 1 and len(_landings(tmp_root)) == 1


def test_a_refused_ring_is_re_rung_by_recovery_without_a_second_row(tmp_root: Path, landed):
    landed.append("connection refused")
    journal = _journal()
    jn.write(tmp_root, journal)
    followups.reconcile(tmp_root, tmp_root, journal)
    assert state.load(tmp_root, "001-mac-01")["integration"]["notices"]["mgr-a"]["rang"] == "refused"
    land_recover.recover(tmp_root, journal["id"])
    assert len(_rows(tmp_root)) == 1
    assert state.load(tmp_root, "001-mac-01")["integration"]["notices"]["mgr-a"]["rang"] == "accepted"


def test_a_notice_coalesced_into_a_wake_that_fails_stays_owed_and_is_re_rung(tmp_root: Path, landed):
    with peers.inbox_guard(tmp_root, "mgr-a"):
        other = peer_wake.reserve(tmp_root, "mgr-a")  # an ordinary peer send, still attempting
    assert other is not None
    journal = _journal()
    jn.write(tmp_root, journal)
    followups.reconcile(tmp_root, tmp_root, journal)
    assert (
        state.load(tmp_root, "001-mac-01")["integration"]["notices"]["mgr-a"]["rang"] == "coalesced_pending"
    )
    assert jn.read(tmp_root, journal["id"])["obligations"]["notices"] == "pending"
    peer_wake.settle(tmp_root, "mgr-a", other, False)
    land_recover.recover(tmp_root, journal["id"])
    assert len(_rows(tmp_root)) == 1
    assert jn.read(tmp_root, journal["id"])["obligations"]["notices"] == "done"


def test_a_notice_into_an_accepted_wake_completes_without_a_ring(
    tmp_root: Path, landed, monkeypatch: pytest.MonkeyPatch
):
    rung: list[str] = []
    monkeypatch.setattr("agent_cli.ops.peers._ring_socket", lambda path: rung.append(path))
    with peers.inbox_guard(tmp_root, "mgr-a"):
        other = peer_wake.reserve(tmp_root, "mgr-a")
    assert other is not None
    peer_wake.settle(tmp_root, "mgr-a", other, True)
    assert followups.reconcile(tmp_root, tmp_root, _journal())["notices"] == "done"
    assert rung == []


def test_a_later_landing_of_the_same_branch_gets_a_new_notice(tmp_root: Path, landed):
    followups.reconcile(tmp_root, tmp_root, _journal("20261009T010000-aaaaaa"))
    followups.reconcile(tmp_root, tmp_root, _journal("20261009T020000-bbbbbb"))
    assert len(_rows(tmp_root)) == 2
    assert len(_landings(tmp_root)) == 2


def test_the_milestone_is_recorded_once_across_a_stop_a_failure_and_a_journal_write(
    tmp_root: Path, landed, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli import journal as journal_mod

    journal = _journal()
    pending.record(tmp_root, "landing", journal["id"], "", key=journal["id"])  # appended, then stopped
    followups.reconcile(tmp_root, tmp_root, journal)
    assert len(_landings(tmp_root)) == 1

    second = _journal("20261009T030000-cccccc")
    real_open = Path.open

    def no_pending(self: Path, *a, **k):
        if ".agent/journal/pending" in self.as_posix():
            raise OSError("read-only")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", no_pending)
    assert followups.reconcile(tmp_root, tmp_root, second)["milestone"] == "pending"
    monkeypatch.setattr(Path, "open", real_open)

    from datetime import date

    journal_mod.write_generated(tmp_root, date(2026, 10, 9), "- the day\n")  # clears, keeps a receipt
    assert followups.reconcile(tmp_root, tmp_root, journal)["milestone"] == "recorded"
    assert _landings(tmp_root) == []  # already journaled: nothing re-added
    assert followups.reconcile(tmp_root, tmp_root, second)["milestone"] == "recorded"
    assert len(_landings(tmp_root)) == 1


def test_a_batch_landing_gives_one_milestone(tmp_root: Path, landed):
    journal = _journal(
        members=[{"branch": BRANCH, "tip": "d" * 40}, {"branch": "agent/other", "tip": "e" * 40}]
    )
    followups.reconcile(tmp_root, tmp_root, journal)
    assert len(_landings(tmp_root)) == 1


def test_aborted_and_legacy_journals_owe_nothing(tmp_root: Path, landed):
    aborted = _journal("20261009T040000-dddddd", state="aborted")
    legacy = {k: v for k, v in _journal("20261009T050000-eeeeee").items() if k != "obligations"}
    for journal in (aborted, legacy):
        jn.write(tmp_root, journal)
        land_recover.recover(tmp_root, journal["id"])
        assert not followups.outstanding(journal)
    assert _landings(tmp_root) == []
    assert _rows(tmp_root) == []


# ----------------------------------------------------------------
def test_a_failed_integration_mark_keeps_the_notice_owed(tmp_root: Path, landed, monkeypatch):
    real_save = state.save

    def no_save(root, record):
        if record.get("id") == "001-mac-01" and "integration" in record:
            raise OSError("disk full")
        return real_save(root, record)

    monkeypatch.setattr(state, "save", no_save)
    journal = _journal()
    jn.write(tmp_root, journal)
    followups.reconcile(tmp_root, tmp_root, journal)
    assert jn.read(tmp_root, journal["id"])["obligations"]["notices"] == "pending"
    monkeypatch.setattr(state, "save", real_save)
    land_recover.recover(tmp_root, journal["id"])
    assert len(_rows(tmp_root)) == 1
    assert jn.read(tmp_root, journal["id"])["obligations"]["notices"] == "done"


def test_recovering_an_older_landing_never_takes_back_a_newer_one(tmp_root: Path, landed):
    landed.append("connection refused")
    old = _journal("20261009T010000-aaaaaa")
    jn.write(tmp_root, old)
    followups.reconcile(tmp_root, tmp_root, old)  # its notice refused, still owed
    new = _journal("20261009T020000-bbbbbb", cand="f" * 40)
    followups.reconcile(tmp_root, tmp_root, new)
    rows = len(_rows(tmp_root))
    land_recover.recover(tmp_root, old["id"])
    integration = state.load(tmp_root, "001-mac-01")["integration"]
    assert integration["journal"] == new["id"] and integration["main"] == "f" * 40
    assert len(_rows(tmp_root)) == rows


def test_an_unresolvable_feature_does_not_hold_back_the_batch(tmp_root: Path, landed):
    state.save(
        tmp_root,
        {
            "id": "002-mac-01",
            "repo": "Harness",
            "branch": "agent/099-gone",
            "state": "done",
            "feature": 99,
            "tasks": [1],
        },
    )
    journal = _journal(
        members=[{"branch": "agent/099-gone", "tip": "d" * 40}, {"branch": BRANCH, "tip": "e" * 40}]
    )
    assert followups.reconcile(tmp_root, tmp_root, journal)["notices"] == "done"
    assert len(_rows(tmp_root)) == 1


def test_same_second_landings_are_ordered_by_git_not_by_id(
    tmp_root: Path, landed, tmp_path_factory, monkeypatch: pytest.MonkeyPatch
):
    """Landing ids sort by second, then a random suffix, so an older landing's
    id can sort after a newer one's. Main only moves forward, so ancestry decides."""
    import subprocess

    repo = tmp_path_factory.mktemp("main")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=F", "-c", "user.email=f@example.test", *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "older landing")
    older_sha = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "newer landing")
    newer_sha = git("rev-parse", "HEAD")
    monkeypatch.setattr("agent_cli.land.main_of", lambda root: repo)
    older = _journal("20261009T020000-ffffff", cand=older_sha)
    newer = _journal("20261009T020000-000000", cand=newer_sha)
    followups.reconcile(tmp_root, tmp_root, older)
    followups.reconcile(tmp_root, tmp_root, newer)
    assert state.load(tmp_root, "001-mac-01")["integration"]["main"] == newer_sha
    followups.reconcile(tmp_root, tmp_root, older)  # recovering the older landing afterwards
    assert state.load(tmp_root, "001-mac-01")["integration"]["main"] == newer_sha
    assert len(_rows(tmp_root)) == 2


def test_the_landing_session_is_the_sender_so_its_own_notice_is_not_rung(tmp_root: Path, landed, monkeypatch):
    rung: list[str] = []
    monkeypatch.setattr("agent_cli.ops.peers._ring_socket", lambda path: rung.append(path))
    obligations = followups.reconcile(tmp_root, tmp_root, _journal(session="mgr-a"))
    assert obligations["notices"] == "done"
    assert rung == [] and len(_rows(tmp_root)) == 1
    assert state.load(tmp_root, "001-mac-01")["integration"]["notices"]["mgr-a"]["rang"] == "unrung"
