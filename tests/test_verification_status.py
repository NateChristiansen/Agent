"""Reading a verification run's state back, while the runner is still writing it.

Split from test_verification_runner.py at the 500-line gate. Real worktrees and real process
boundaries, same as the runner tests; no model or network calls.
"""

from __future__ import annotations

import calendar
import os
import subprocess
import time

import pytest

from agent_cli.dispatch import verification as runner
from agent_cli.dispatch import verification_status as status_mod
from agent_cli.dispatch.verification_store import Store
from verification_fixtures import assessment, config, fixture, start  # noqa: F401 — pytest fixture


def test_partial_prefix_is_durable_and_unaccepted(fixture):  # noqa: F811 — the fixture
    settings = config("true", "sleep 60", command_timeout=10)
    handle = start(fixture, settings)
    deadline = time.monotonic() + 8
    while True:
        status = runner.load_status(handle.root, handle.binding)
        if status.lifecycle["command_index"] == 1:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert status.evidence is not None and len(status.evidence.commands) == 1
    assert status.active and status.requires_lease
    assert not assessment(fixture, status, settings).verified
    runner.request_cancel(handle.root, handle.binding)
    result = runner.wait(handle)
    assert result.cleanup_confirmed


def test_cancelling_a_running_command_finishes_the_lifecycle(fixture):  # noqa: F811 — the fixture
    """A cancel landing while a command runs used to give the reason `command_cancelled`, which
    the store refuses, so the guardian's last lifecycle write raised after terminal evidence."""
    handle = start(fixture, config("sleep 60", command_timeout=10))
    deadline = time.monotonic() + 8
    while runner.load_status(handle.root, handle.binding).lifecycle["phase"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    runner.request_cancel(handle.root, handle.binding)
    result = runner.wait(handle)
    assert result.evidence is not None and result.evidence.state == "cancelled"
    store = Store(handle.root, handle.binding)
    try:
        life = store.load_lifecycle()
    finally:
        store.close()
    assert life["finished"] and life["reason"] == "cancelled"


def test_a_child_that_died_past_the_deadline_is_a_timeout_not_an_exit():
    """The wait loop checks the deadline only at its top, so a watchdog kill landing inside the
    select window came back as an ordinary exit and was recorded `exited` with a signal code.
    Reproducing that through the real processes needs the child reaped before select returns,
    which is exactly the ordering a loaded machine changes, so the decision is tested directly.
    """
    assert runner._exit_outcome(time.monotonic() + 30, 0) == ("exited", 0)
    assert runner._exit_outcome(time.monotonic() + 30, -9) == ("exited", -9)
    assert runner._exit_outcome(time.monotonic() - 0.001, -9) == ("timeout", None)


def _lifecycle_reader(monkeypatch, snapshots):
    """Make Store.load_lifecycle hand out `snapshots` in order, then the real record."""
    real = Store.load_lifecycle
    queue = list(snapshots)

    def load(self):
        return queue.pop(0) if queue else real(self)

    monkeypatch.setattr(Store, "load_lifecycle", load)


def test_a_lifecycle_that_moved_mid_read_is_retried_not_called_stale(fixture, monkeypatch):  # noqa: F811
    """load_status reads lifecycle.json and then cleanup-N.json. The runner writes both while
    it works, so a snapshot taken before a command's watchdog pid was recorded, paired with the
    cleanup file that watchdog wrote a moment later, disagrees. That is two instants, not a
    stale record: it used to raise `invalid or stale verifier cleanup record` and fail the run.
    """
    handle = start(fixture, config("true"))
    result = runner.wait(handle)
    assert result.cleanup_confirmed
    settled = Store(handle.root, handle.binding).load_lifecycle()
    assert settled["command_index"] == 0 and settled["watchdog_pid"] is not None

    # the snapshot as it looked one write earlier, before `life["watchdog_pid"] = watchdog.pid`
    mid_write = dict(settled, watchdog_pid=None)
    _lifecycle_reader(monkeypatch, [mid_write])
    status = runner.load_status(handle.root, handle.binding)
    assert status.lifecycle == settled

    # but a lifecycle that is not moving and still disagrees is a real invalid record
    _lifecycle_reader(monkeypatch, [mid_write] * 5)
    with pytest.raises(ValueError, match="invalid or stale verifier cleanup record"):
        runner.load_status(handle.root, handle.binding)


posix_only = pytest.mark.skipif(os.name == "nt", reason="start-time identity is POSIX only")


@pytest.fixture
def stranger():
    """An unrelated live process leading its own group, as a reused pid would be."""
    process = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()


def _rewrite(handle, *, backdate: bool, **changes):
    """Rewrite a settled single-command run as a store from before this change would hold it,
    optionally moving its file times an hour back, to before `stranger` started."""
    store = Store(handle.root, handle.binding)
    try:
        life = {k: v for k, v in store.load_lifecycle().items() if k not in {"schema_version", "binding"}}
        life.update(changes)
        cleanup = store.read("cleanup-00000.json")
        assert cleanup is not None
        cleanup.update(watchdog_pid=life["watchdog_pid"], command_pgid=life["command_pid"])
        (store.path / "cleanup-00000.json").unlink()
        store.write("cleanup-00000.json", cleanup, immutable=True)
        store.lifecycle(life)
        if backdate:
            past = time.time() - 3600
            for name in ("lifecycle.json", "cleanup-00000.json"):
                os.utime(store.path / name, (past, past))
    finally:
        store.close()


@posix_only
@pytest.mark.parametrize("role", ["guardian_pid", "command_pid", "watchdog_pid"])
@pytest.mark.parametrize("backdate", [True, False])
def test_a_finished_run_does_not_hold_the_lease_through_a_reused_pid(fixture, stranger, role, backdate):  # noqa: F811
    """A legacy record carries no process identity, only file times. A process that started
    after the write naming its pid cannot be the run's, so an unrelated process that later got
    the number must not keep a finished run's lease. Started before that write, the same
    process could be the run's own and keeps the lease."""
    handle = start(fixture, config("true"))
    assert runner.wait(handle).cleanup_confirmed
    _rewrite(handle, backdate=backdate, **{role: stranger})
    status = runner.load_status(handle.root, handle.binding)
    assert status.lifecycle["finished"] and status.lifecycle[role] == stranger
    assert status.requires_lease is not backdate
    if backdate:
        assert not status.active and status.cleanup_confirmed


@posix_only
@pytest.mark.parametrize("unknown", ["start", "mtime"])
def test_an_unfinished_run_with_an_unknown_process_keeps_the_lease(fixture, stranger, monkeypatch, unknown):  # noqa: F811
    handle = start(fixture, config("true"))
    assert runner.wait(handle).cleanup_confirmed
    _rewrite(handle, backdate=True, guardian_pid=stranger, phase="running", finished=False)
    if unknown == "start":
        monkeypatch.setattr(status_mod, "_lstart", lambda fields: None)
    else:
        monkeypatch.setattr(Store, "modified", lambda self, name: None)
    status = runner.load_status(handle.root, handle.binding)
    assert status.active and status.requires_lease


@posix_only
def test_a_watchdog_with_an_unreadable_cleanup_time_is_judged_by_its_number(
    fixture,  # noqa: F811
    stranger,
    monkeypatch,
):
    """A cleanup record whose time cannot be read leaves the watchdog's identity unknown, and
    the lifecycle time does not stand in for it: the number alone decides, so a live process
    under that number keeps the lease."""
    handle = start(fixture, config("true"))
    assert runner.wait(handle).cleanup_confirmed
    _rewrite(handle, backdate=True, watchdog_pid=stranger)
    real = Store.modified
    monkeypatch.setattr(
        Store, "modified", lambda self, name: None if name.startswith("cleanup-") else real(self, name)
    )
    status = runner.load_status(handle.root, handle.binding)
    assert not status.active and not status.cleanup_confirmed and status.requires_lease


def test_lstart_reads_ps_output_as_utc():
    stamp = calendar.timegm((2026, 10, 10, 2, 3, 39, 0, 0, 0))
    assert status_mod._lstart("Sat Oct 10 02:03:39 2026".split()) == stamp
    assert status_mod._lstart("Fri Oct  9 23:44:12 2026".split()) == stamp - 8367
    assert status_mod._lstart(["garbage"]) is None
