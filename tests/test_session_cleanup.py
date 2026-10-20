"""Safe session cleanup.

The owner's Windows run had seven stale session records and no safe way to clear them:
`session prune --days 0` would also have deleted the live sessions, and nothing removed one
record or a wrong by-pid entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_cli import ops, sessions
from agent_cli.core import AgentError, NotFound
from agent_cli.ops import sessions as ops_sessions

LIVE, DEAD = 111, 222


@pytest.fixture(autouse=True)
def _liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_sessions, "is_alive", lambda pid: pid == LIVE)


def _ids(root: Path) -> set[str]:
    return {str(r["session_id"]) for r in sessions.all_records(root)}


def test_age_prune_never_removes_a_live_session(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions.set_record(tmp_root, "live", pid=LIVE)
    sessions.set_record(tmp_root, "dead", pid=DEAD)
    monkeypatch.setenv("AGENT_NOW", "2099-01-01T00:00:00+00:00")  # both records are now old
    op = ops.session_prune(tmp_root, days=0)
    assert op.data["removed"] == ["dead"]
    assert _ids(tmp_root) == {"live"}


def test_dead_prune_keeps_live_and_unknown_pid_records(tmp_root: Path) -> None:
    sessions.set_record(tmp_root, "live", pid=LIVE)
    sessions.set_record(tmp_root, "dead", pid=DEAD)
    sessions.set_record(tmp_root, "unknown")  # no pid: unknown is not dead
    op = ops.session_prune(tmp_root, dead=True)
    assert op.data["removed"] == ["dead"]
    assert _ids(tmp_root) == {"live", "unknown"}


def test_rm_refuses_a_live_session_without_force(tmp_root: Path) -> None:
    sessions.set_record(tmp_root, "live", pid=LIVE)
    with pytest.raises(AgentError, match="is live"):
        ops.session_rm(tmp_root, session_id="live")
    assert _ids(tmp_root) == {"live"}
    ops.session_rm(tmp_root, session_id="live", force=True)
    assert _ids(tmp_root) == set()


def test_rm_clears_only_the_by_pid_entries_that_still_name_it(tmp_root: Path) -> None:
    sessions.set_record(tmp_root, "old", pid=DEAD)
    sessions.set_record(tmp_root, "other", pid=LIVE)
    sessions.write_by_pid(tmp_root, DEAD, "old")
    sessions.write_by_pid(tmp_root, 333, "old")
    sessions.write_by_pid(tmp_root, LIVE, "other")  # another session's entry stays
    op = ops.session_rm(tmp_root, session_id="old")
    assert sorted(op.data["by_pid"]) == ["222", "333"]
    assert sessions.read_by_pid(tmp_root, DEAD) is None
    assert sessions.read_by_pid(tmp_root, LIVE) == "other"
    # The lock file stays: another process may hold it (see remove's docstring).
    assert (sessions.sessions_dir(tmp_root) / "old.lock").exists()


def test_age_prune_keeps_an_old_record_with_unknown_process(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.set_record(tmp_root, "daemon-hosted")  # no pid: identity unknown, not dead
    monkeypatch.setenv("AGENT_NOW", "2099-01-01T00:00:00+00:00")
    assert ops.session_prune(tmp_root, days=0).data["removed"] == []
    assert _ids(tmp_root) == {"daemon-hosted"}


def test_removal_keeps_the_lock_file_and_rechecks_under_it(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.set_record(tmp_root, "s", pid=DEAD)
    lock = sessions.sessions_dir(tmp_root) / "s.lock"
    assert lock.exists()
    # The recheck runs on the record as read under the lock: a session that came back to life
    # between the scan and the delete stays.
    assert sessions.remove(tmp_root, "s", keep=lambda record: True) is None
    assert _ids(tmp_root) == {"s"}
    assert sessions.remove(tmp_root, "s", keep=lambda record: False) == []
    assert _ids(tmp_root) == set() and lock.exists()


def test_a_by_pid_write_waits_for_a_removal_in_progress(tmp_root: Path) -> None:
    import threading

    sessions.write_by_pid(tmp_root, DEAD, "old")
    wrote = threading.Event()

    def reassign() -> None:
        sessions.write_by_pid(tmp_root, DEAD, "new")
        wrote.set()

    with sessions._by_pid_locked(tmp_root):
        writer = threading.Thread(target=reassign)
        writer.start()
        assert not wrote.wait(0.3), "write_by_pid ran while the by-pid lock was held"
    writer.join(5)
    assert wrote.is_set() and sessions.read_by_pid(tmp_root, DEAD) == "new"
    # Removing "old" now must not touch the entry reassigned to "new".
    sessions.set_record(tmp_root, "old", pid=DEAD)
    assert sessions.remove(tmp_root, "old") == []
    assert sessions.read_by_pid(tmp_root, DEAD) == "new"


def test_session_prune_by_age_through_the_cli(tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch) -> None:
    # Moved from test_sessions_capture.py. The old record now carries a process that has exited:
    # a pid-less record is unknown, not dead, and prune keeps it.
    monkeypatch.setattr(ops_sessions, "is_alive", lambda pid: False)
    run(["session", "set", "--session-id", "old"])
    sessions.set_field(tmp_root, "old", "pid", 999_999_999)
    monkeypatch.setenv("AGENT_NOW", "2026-10-03T14:05:00-07:00")
    run(["session", "set", "--session-id", "new"])
    r = run(["--json", "session", "prune", "--days", "14"])
    assert r.json["removed"] == ["old"]
    assert (tmp_root / ".agent/sessions/new.json").exists()


def test_removal_refuses_without_file_locking_and_changes_nothing(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even on a future unsupported platform, removal must fail closed.
    sessions.set_record(tmp_root, "s", pid=DEAD)
    sessions.write_by_pid(tmp_root, DEAD, "s")
    monkeypatch.setattr(sessions, "_file_lock_supported", lambda: False)
    for attempt in (
        lambda: ops.session_rm(tmp_root, session_id="s", force=True),
        lambda: ops.session_prune(tmp_root, dead=True),
    ):
        with pytest.raises(AgentError, match="file locking"):
            attempt()
    assert _ids(tmp_root) == {"s"}
    assert sessions.read_by_pid(tmp_root, DEAD) == "s"


@pytest.mark.skipif(sys.platform == "win32", reason="this probe uses POSIX flock")
def test_by_pid_cleanup_happens_while_the_session_lock_is_still_held(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.set_record(tmp_root, "s", pid=DEAD)
    sessions.write_by_pid(tmp_root, DEAD, "s")
    held: list[str] = []
    real = sessions._by_pid_locked

    def watch(root: Path):
        # A second non-blocking attempt on <sid>.lock fails only if remove still holds it.
        import fcntl

        with open(sessions.sessions_dir(root) / "s.lock", "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append("free")
                fcntl.flock(fh, fcntl.LOCK_UN)
            except BlockingIOError:
                held.append("held")
        return real(root)

    monkeypatch.setattr(sessions, "_by_pid_locked", watch)
    assert sessions.remove(tmp_root, "s") == [str(DEAD)]
    assert held == ["held"]


def test_rm_of_an_unknown_session_is_not_found(tmp_root: Path) -> None:
    with pytest.raises(NotFound):
        ops.session_rm(tmp_root, session_id="nope")
