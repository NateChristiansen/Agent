"""Shared fixtures for the notice producer and recovery tests: a live local
launcher with an inbox key, a recording fake transport and guard tracking. Nothing here rings a
real session or writes outside the temporary root."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli import features, leases, ops, peers, sessions
from agent_cli.core import AgentError
from agent_cli.dispatch import guards, lifecycle, notice_delivery, notices, terminal, verification_gate
from agent_cli.dispatch.state import load, save
from agent_cli.ops import peer_wake
from dispatch_helpers import DEAD_PID, _new, envelope

MGR = "mgr-1"
OTHER = "mgr-2"


def _no_real_ring(*args, **kwargs):
    raise AssertionError("a test reached a real transport")


def launcher(root: Path, sid: str = MGR) -> None:
    sessions.set_record(root, sid, name=f"{sid}-name", pid=os.getpid())
    sessions.set_field(root, sid, "provider", "claude")
    sessions.set_field(root, sid, "inbox", str(root / f"{sid}.sock"))


@pytest.fixture
def wired(droot: Path, monkeypatch: pytest.MonkeyPatch):
    """Rings on through a recording transport, live launchers, and every guard tracked."""
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.setenv("AGENT_NOTIFY", "on")
    monkeypatch.setattr(ops.peers, "inbox_token", lambda address: "token")
    monkeypatch.setattr(ops.peers, "_ring_socket", _no_real_ring)
    monkeypatch.setattr(ops.peers, "_run", _no_real_ring)
    launcher(droot, MGR)
    launcher(droot, OTHER)

    held: list[str] = []
    lock = threading.Lock()
    real = guards.area_guard

    @contextmanager
    def tracking(root, area, **kwargs):
        with real(root, area, **kwargs):
            with lock:
                held.append(area)
            try:
                yield
            finally:
                with lock:
                    held.remove(area)

    monkeypatch.setattr(guards, "area_guard", tracking)
    monkeypatch.setattr(verification_gate, "area_guard", tracking)

    env = SimpleNamespace(root=droot, rings=[], held=held, answer=(True, "rang", None), on_ring=None)

    def ring(target: dict[str, Any], name: str):
        [item] = notice_delivery.pending(droot, str(target["session_id"]))[-1:]
        envelope = item["envelope"]
        env.rings.append(
            {
                "session": str(target["session_id"]),
                "held": list(held),
                "lease": leases.get(droot, "repo-slot:alpha/0"),
                "envelope": envelope,
                "record": load(droot, envelope["dispatch_id"]),
            }
        )
        if env.on_ring is not None:
            env.on_ring(envelope)
        return env.answer

    monkeypatch.setattr(notice_delivery, "ring_dispatch", ring)
    return env


def ready(root: Path, monkeypatch: pytest.MonkeyPatch, *, session: str = MGR, **kw: Any) -> dict:
    """A launched dispatch whose worker returned done, not yet finalized."""
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)
    kw.setdefault("no_checks", True)
    d = _new(root, session_id=session, **kw)
    ret = envelope()
    ret["session_id"] = d["session_id"]
    ret["structured_output"]["nonce"] = d["nonce"]
    d.update(
        state="running",
        supervisor_pid=DEAD_PID,
        stop_reason="result",
        exit_code=0,
        last_result=ret,
        worker_cleanup_pending=False,
    )
    save(root, d)
    return d


def finish(root: Path, d: dict, outcome: dict | None = None) -> dict:
    from agent_cli import dispatch, repos

    if outcome is None:
        if not load(root, d["id"]).get("verification"):
            verification_gate.execute(root, d["id"], d["attempt"])
        outcome = dispatch.evaluate(root, load(root, d["id"]), repos.load_registry(root).require(d["repo"]))
    terminal.finalize(root, load(root, d["id"]), features.resolve(root, str(d["feature"])), outcome)
    return load(root, d["id"])


def broken_hold(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """Make the hold-start intent save fail: its record guard busy, or its save raising."""
    if how == "save":
        from agent_cli.dispatch import notice_producers

        def raising(root, record):
            raise OSError("disk full")

        monkeypatch.setattr(notice_producers, "save", raising)
        return
    real = verification_gate.record_guard

    def busy(root, dispatch_id, **kwargs):
        if sys._getframe(1).f_code.co_name == "question_hold":
            raise AgentError("harness mutation guard is busy; retry after the current operation")
        return real(root, dispatch_id, **kwargs)

    monkeypatch.setattr(verification_gate, "record_guard", busy)


def answer_at_hold(root: Path, did: str, answer: Callable[[], object]) -> tuple[threading.Thread, dict]:
    """Once the worker's question is on the record and its hold has had time to start, keep a
    copy of the record and answer the question."""
    seen: dict[str, Any] = {}

    def run() -> None:
        for _ in range(500):
            try:
                d = load(root, did)
            except AgentError:
                time.sleep(0.02)
                continue
            returned = (d.get("last_result") or {}).get("structured_output") or {}
            if returned.get("status") == "question" and d["state"] == "running":
                time.sleep(0.2)
                seen["record"] = load(root, did)
                answer()
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, seen


def reservation(root: Path, sid: str = MGR) -> dict:
    """The recipient's shared wake reservation."""
    from agent_cli import peers

    return (sessions.get(root, sid) or {}).get(peers.WAKE_FIELD) or {}


def queued(root: Path, sid: str = MGR) -> list[str]:
    """Notice ids in the recipient's store, oldest arrival first."""
    return [item["envelope"]["notice_id"] for item in notice_delivery.pending(root, sid)]


def entries(record: dict) -> list[dict]:
    return sorted(notices.outbox(record).values(), key=lambda e: e["generation"])


@contextmanager
def held_elsewhere(guard):
    """Hold `guard` on another thread until the block ends."""
    taken, release = threading.Event(), threading.Event()

    def hold():
        with guard:
            taken.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    taken.wait(5)
    try:
        yield
    finally:
        release.set()
        worker.join()


def peer_send(root: Path, text: str) -> str | None:
    """`ops.peer_send`'s append and reservation, without resolving or ringing a session."""
    with peers.inbox_guard(root, MGR):
        peers.send(root, to=MGR, text=text, frm="mgr-2", frm_name="mgr-two")
        wake_id = peer_wake.reserve(root, MGR)
    if wake_id:
        peer_wake.settle(root, MGR, wake_id, True)
    return wake_id
