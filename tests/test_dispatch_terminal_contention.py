"""Real terminal-lock contention and recovery of a verified return after retry exhaustion."""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from agent_cli import features, repos
from agent_cli.dispatch import evaluate, notice_producers, terminal, verification_gate
from agent_cli.dispatch.state import load, save
from dispatch_helpers import droot as droot
from notice_helpers import entries, finish, queued, ready, wired  # noqa: F401


@contextmanager
def held_by_process(root: Path, dispatch_id: str, seconds: float):
    script = """
import sys, time
from pathlib import Path
from agent_cli.dispatch.verification_gate import record_guard
with record_guard(Path(sys.argv[1]), sys.argv[2]):
    print('held', flush=True)
    time.sleep(float(sys.argv[3]))
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(root), dispatch_id, str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        yield
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=10)


HOLD_S = 1.5


@pytest.fixture
def short_guard(monkeypatch):
    """A guard that gives up after 0.5 s, so a 1.5 s hold outlasts it three times over: the
    retry-past-a-busy-guard paths without five real seconds each."""
    monkeypatch.setattr(verification_gate, "GUARD_TIMEOUT_S", 0.5)


def verified_outcome(wired, monkeypatch):
    d = ready(wired.root, monkeypatch, no_checks=False)
    verification_gate.execute(wired.root, d["id"], d["attempt"])
    d = load(wired.root, d["id"])
    outcome = evaluate(wired.root, d, repos.load_registry(wired.root).require(d["repo"]))
    return d, features.resolve(wired.root, str(d["feature"])), outcome


def test_a_guard_held_past_its_timeout_retries_and_delivers_verified_wake(wired, monkeypatch, short_guard):
    d, feat, outcome = verified_outcome(wired, monkeypatch)
    began = time.monotonic()
    with held_by_process(wired.root, d["id"], HOLD_S):
        after = terminal.finalize(wired.root, d, feat, outcome)
    assert time.monotonic() - began >= HOLD_S
    assert after["state"] == "done" and after["terminal_committed"]
    assert after["verification_result"]["status"] == "verified"
    [entry] = entries(after)
    assert entry["delivery"] == "wake-accepted" and len(wired.rings) == 1


@pytest.mark.parametrize("fence", [None, "cancel", "attempt"])
def test_exhausted_retries_keep_intent_and_pending_notice_recovery(wired, monkeypatch, fence):
    d, feat, outcome = verified_outcome(wired, monkeypatch)
    with held_by_process(wired.root, d["id"], 5):
        after = terminal.finalize(wired.root, d, feat, outcome, retry_budget=0.05)
        pending = terminal.pending_path(wired.root, d)
        intent = json.loads(pending.read_text())["dispatch"]
        assert after["state"] == "running"
        assert intent["result"]["status"] == "done"
        assert intent["verification_result"]["status"] == "verified"
        assert entries(intent)[0]["envelope"]["state"] == "done"
        assert wired.rings == []
    if fence:
        with verification_gate.record_guard(wired.root, d["id"]) as current:
            if fence == "cancel":
                current["state"] = "cancelled"
            else:
                current["attempt"] += 1
            save(wired.root, current)
    notice_producers.reconcile(wired.root, [d["id"]])
    after = load(wired.root, d["id"])
    assert not pending.exists()
    if fence:
        assert after["state"] == ("cancelled" if fence == "cancel" else "running")
        assert wired.rings == []
    else:
        assert after["state"] == "done" and after["terminal_committed"]
        assert after["result"]["status"] == "done"
        assert after["verification_result"]["status"] == "verified"
        assert entries(after)[0]["notice_id"] in queued(wired.root)
        assert len(wired.rings) == 1


def counted_publish(monkeypatch) -> list[str]:
    published: list[str] = []
    real = verification_gate.publish

    def counting(root, d, binding, mode, event_id):
        published.append(mode)
        real(root, d, binding, mode, event_id)

    monkeypatch.setattr(verification_gate, "publish", counting)
    return published


def test_execute_waits_out_a_guard_held_past_its_timeout(wired, monkeypatch, short_guard):
    d = ready(wired.root, monkeypatch, no_checks=False)
    published = counted_publish(monkeypatch)
    began = time.monotonic()
    with held_by_process(wired.root, d["id"], HOLD_S):
        verification_gate.execute(wired.root, d["id"], d["attempt"])
    assert time.monotonic() - began >= HOLD_S
    after = finish(wired.root, d)
    assert after["state"] == "done" and after["terminal_committed"]
    assert after["verification_result"]["status"] == "verified"
    assert len(published) == 1


def test_supervisor_failure_settles_past_a_busy_guard_and_finalizes(wired, monkeypatch, short_guard):
    from agent_cli.dispatch import supervisor

    d = ready(wired.root, monkeypatch, no_checks=False)
    d["state"] = "queued"
    save(wired.root, d)
    published = counted_publish(monkeypatch)
    real_execute = verification_gate.execute

    def launch(*args):
        with verification_gate.record_guard(wired.root, d["id"]) as current:
            current.update(state="running", supervisor_pid=os.getpid())
            save(wired.root, current)
            return current

    with ExitStack() as holder:

        def execute_then_fail(root, dispatch_id, attempt):
            real_execute(root, dispatch_id, attempt)
            holder.enter_context(held_by_process(root, dispatch_id, HOLD_S))
            raise RuntimeError("injected after verification")

        monkeypatch.setattr(supervisor, "_launch_attempt", launch)
        monkeypatch.setattr(verification_gate, "execute", execute_then_fail)
        with pytest.raises(RuntimeError, match="injected"):
            supervisor.run(wired.root, dispatch_id=d["id"])
    after = load(wired.root, d["id"])
    assert after["state"] == "failed" and after["terminal_committed"]
    assert len(published) == 1


def test_slow_render_does_not_hold_same_repo_finalizer(wired, monkeypatch):
    first = ready(wired.root, monkeypatch, writes=["one.txt"])
    second = ready(wired.root, monkeypatch, writes=["two.txt"])
    entered, release = threading.Event(), threading.Event()
    errors = []
    real = terminal.render
    caller = threading.get_ident()

    def slow(root):
        assert not wired.held
        if threading.get_ident() != caller:
            entered.set()
            assert release.wait(10)
        real(root)

    def finalize_first():
        try:
            finish(wired.root, first)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(terminal, "render", slow)
    worker = threading.Thread(target=finalize_first)
    worker.start()
    try:
        assert entered.wait(10)
        after = finish(wired.root, second)
        assert after["state"] == "done" and after["terminal_committed"]
        assert entries(after)[0]["notice_id"] in queued(wired.root)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    for d in (first, second):
        after = load(wired.root, d["id"])
        assert after["terminal_committed"]
        assert entries(after)[0]["notice_id"] in queued(wired.root)


def test_notification_merges_audit_without_overwriting_a_concurrent_read(wired, monkeypatch):
    import importlib

    notifications = importlib.import_module("agent_cli.dispatch.notify")
    d = ready(wired.root, monkeypatch)
    d["notify"] = ["chat", "macos"]
    save(wired.root, d)

    def read_during_delivery(*args):
        with verification_gate.record_guard(wired.root, d["id"]) as current:
            current["chat_pending"] = False
            current["notification_probe"] = "concurrent read kept"
            save(wired.root, current)
        return "sent"

    monkeypatch.setattr(notifications, "_macos_notify", read_during_delivery)
    after = finish(wired.root, d)
    assert after["state"] == "done" and after["terminal_committed"]
    assert after["notification_probe"] == "concurrent read kept"
    assert after["chat_pending"] is False
    assert after["notify_log"][-1]["results"]["macos"] == "sent"


def test_deferred_and_failed_publication_still_queue_worker_retention(wired, monkeypatch):
    from agent_cli.hooks import transcript_retention

    queued_ids: list[str] = []
    monkeypatch.setattr(
        transcript_retention, "enqueue", lambda root, record: queued_ids.append(record["dispatch_id"])
    )
    d, feat, outcome = verified_outcome(wired, monkeypatch)
    with held_by_process(wired.root, d["id"], 5):
        terminal.finalize(wired.root, d, feat, outcome, retry_budget=0.05)
    assert queued_ids == [d["id"]]  # publication deferred behind a busy guard; retention is not

    def broken(*args):
        raise RuntimeError("publication failed")

    with monkeypatch.context() as patch:
        patch.setattr(terminal, "_publish", broken)
        with pytest.raises(RuntimeError, match="publication failed"):
            terminal.finalize(wired.root, d, feat, outcome)
    assert queued_ids == [d["id"], d["id"]]
    terminal.recover_pending(wired.root, [d["id"]])  # the deferral's retry already queued it
    assert queued_ids == [d["id"], d["id"]] and not terminal.pending_path(wired.root, d).exists()


def test_a_retention_failure_never_blocks_the_terminal_commit(wired, monkeypatch, capsys):
    from agent_cli.hooks import transcript_retention

    def unavailable(root, record):
        raise OSError("spool unwritable")

    monkeypatch.setattr(transcript_retention, "enqueue", unavailable)
    d, feat, outcome = verified_outcome(wired, monkeypatch)
    after = terminal.finalize(wired.root, d, feat, outcome)
    assert after["state"] == "done" and after["terminal_committed"]
    assert f"transcript retention not queued for {d['id']} (spool unwritable)" in capsys.readouterr().err
