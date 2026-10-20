"""Process-level crash and concurrency barriers for the cross-device receiver.

Each child process imports the receiver and patches one internal to block or `os._exit` at a
barrier; production code has no fault switch. The parent then retries and checks the
documented outcome: a clean retry adds nothing, an ambiguous crash window
may duplicate, and nothing is appended or rung after the expiry window.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from agent_cli import peers
from agent_cli.peer_net import receive
from agent_cli.peer_net import store as store_mod
from peer_net_helpers import NOW, clock, enable, envelope, raw, recipient

PRELUDE = """
import json, os, sys, time
from datetime import datetime, timedelta
from pathlib import Path
import agent_cli.ops.peers as ops_peers
from agent_cli.peer_net import receiver, store

root = Path(os.environ["AGENT_ROOT"])
work = Path(sys.argv[1])
body = sys.argv[2].encode()
NOW = datetime.fromisoformat("2026-09-03T14:05:00-07:00")
ops_peers.is_alive = lambda pid: True

def ring(target, name, codex_launch=None):
    with (work / "rings").open("a") as fh:
        fh.write("ring\\n")
    return True, "rang"

ops_peers.wake_peer = ring
clock = lambda: NOW

PARENT = os.getppid()

def barrier(name):
    # Bounded: a child whose test died must not spin forever. One outlived its run by 7.5
    # hours on 2026-10-03 and loaded every later suite run.
    (work / f"at-{name}").write_text("1")
    deadline = time.monotonic() + 60
    while not (work / f"go-{name}").exists():
        if time.monotonic() > deadline or os.getppid() != PARENT:
            os._exit(3)
        time.sleep(0.01)
"""

RUN = """
out = receiver.receive(root, "mac", body, clock=clock)
(work / f"out-{os.getpid()}.json").write_text(json.dumps(out))
"""


def _child(work: Path, body: dict, patch: str = "") -> subprocess.Popen:
    script = PRELUDE + patch + RUN
    return subprocess.Popen([sys.executable, "-c", script, str(work), json.dumps(body)])


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"barrier {path.name} never reached"
        time.sleep(0.02)


def _rings(work: Path) -> int:
    path = work / "rings"
    return len(path.read_text().splitlines()) if path.exists() else 0


@pytest.fixture
def setup(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    enable(tmp_root)
    sid = recipient(tmp_root)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)

    def ring(target, name, codex_launch=None):
        with (work / "rings").open("a") as fh:
            fh.write("ring\n")
        return True, "rang"

    monkeypatch.setattr("agent_cli.ops.peers.wake_peer", ring)
    return tmp_root, sid, work


@pytest.fixture
def retry(monkeypatch: pytest.MonkeyPatch):
    """Retry in this process at `at`. AGENT_NOW moves too, so the wake reservation ages the
    way it would in real time."""

    def run(root: Path, body: dict, at=NOW) -> dict:
        monkeypatch.setenv("AGENT_NOW", at.isoformat())
        return receive(root, "mac", raw(body), clock=clock(at))

    return run


def test_a_crash_after_accept_is_finished_by_theretry(setup, retry):
    root, sid, work = setup
    body = envelope()
    child = _child(work, body, "receiver._queue = lambda *a, **k: os._exit(1)\n")
    assert child.wait(30) == 1
    out = retry(root, body)
    assert out["state"] == "already-accepted" and out["recovery"] == "appended" and out["queue"] == "queued"
    assert len(peers.peek(root, sid)) == 1 and _rings(work) == 1


def test_a_crash_after_the_append_finds_the_row_and_does_not_append_again(setup, retry):
    root, sid, work = setup
    body = envelope()
    patch = (
        "real = receiver.peers_mod.send\n"
        "def send(*a, **k):\n    real(*a, **k)\n    os._exit(1)\n"
        "receiver.peers_mod.send = send\n"
    )
    assert _child(work, body, patch).wait(30) == 1
    out = retry(root, body)
    # The row was found, so nothing is appended; the first wake never ran, so the retry rings.
    assert out["recovery"] == "wake_requested" and out["queue"] == "queued"
    assert len(peers.peek(root, sid)) == 1 and _rings(work) == 1


def test_a_drain_inside_the_append_crash_window_may_duplicate_as_documented(setup, retry):
    """D3: after the append and before `queued` commits, a drain leaves nothing to find, so the
    retry appends again. That is the declared ambiguous window, not a clean retry."""
    root, sid, work = setup
    body = envelope()
    patch = (
        "real = receiver.peers_mod.send\n"
        "def send(*a, **k):\n    real(*a, **k)\n    os._exit(1)\n"
        "receiver.peers_mod.send = send\n"
    )
    assert _child(work, body, patch).wait(30) == 1
    assert len(peers.take(root, sid)) == 1
    out = retry(root, body)
    assert out["recovery"] == "appended" and len(peers.peek(root, sid)) == 1


def test_a_crash_after_the_claim_waits_out_the_lease_before_ringing_again(setup, retry):
    root, sid, work = setup
    body = envelope()
    assert _child(work, body, "ops_peers.wake_peer = lambda t, n, c=None: os._exit(1)\n").wait(30) == 1
    held = retry(root, body)
    assert held["wake"] == "in-progress" and _rings(work) == 0
    lapsed = retry(root, body, at=NOW + timedelta(seconds=store_mod.CLAIM_LEASE_S + 1))
    assert lapsed["wake"] == "accepted" and lapsed["recovery"] == "wake_requested" and _rings(work) == 1


def test_a_crash_after_the_ring_is_coalesced_by_the_shared_reservation(setup, retry):
    """D3 allows an extra ring here: the ring went out and its claim never settled. In practice
    the local reservation (`peer_wake`, accepted for 30 minutes) absorbs a retry inside that
    window, so the retry reports `coalesced` and rings nothing. After 30 minutes it would ring
    once more."""
    root, sid, work = setup
    body = envelope()
    patch = "store.settle_wake = lambda *a, **k: os._exit(1)\n"
    assert _child(work, body, patch).wait(30) == 1
    assert _rings(work) == 1
    out = retry(root, body, at=NOW + timedelta(seconds=store_mod.CLAIM_LEASE_S + 1))
    assert out["wake"] == "coalesced" and _rings(work) == 1
    assert retry(root, body, at=NOW + timedelta(seconds=120))["wake"] == "coalesced" and _rings(work) == 1


def test_two_processes_sending_the_same_id_append_once_and_ring_once(setup, retry):
    root, sid, work = setup
    body = envelope()
    gate = work / "go"
    patch = f"while not Path({str(gate)!r}).exists():\n    time.sleep(0.01)\n"
    children = [_child(work, body, patch) for _ in range(2)]
    time.sleep(0.5)
    gate.write_text("go")
    assert [c.wait(30) for c in children] == [0, 0]
    outs = [json.loads(p.read_text()) for p in work.glob("out-*.json")]
    assert sorted(o.get("state", "") for o in outs) == ["accepted", "already-accepted"], outs
    assert len(peers.peek(root, sid)) == 1 and _rings(work) == 1


def test_concurrent_first_connects_all_open_the_store(tmp_root, tmp_path):
    """Four processes opening a fresh store at once: without the init guard, switching to WAL
    failed with "database is locked" in 73 of 160 connects (2026-10-03)."""
    gate = tmp_path / "go"
    script = (
        "import os, sys, time\nfrom pathlib import Path\nfrom agent_cli.peer_net import store\n"
        f"while not Path({str(gate)!r}).exists():\n    time.sleep(0.005)\n"
        "store.connect(Path(os.environ['AGENT_ROOT'])).close()\n"
    )
    children = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(4)]
    time.sleep(0.5)
    gate.write_text("go")
    assert [c.wait(30) for c in children] == [0, 0, 0, 0]


def _release(work: Path, name: str) -> None:
    (work / f"go-{name}").write_text("1")


def test_a_stale_accepted_read_never_appends_after_another_queued_and_drained(setup, retry):
    root, sid, work = setup
    body = envelope()
    patch = (
        "real_queue = receiver._queue\n"
        "def queue(*a, **k):\n    barrier('queue')\n    return real_queue(*a, **k)\n"
        "receiver._queue = queue\n"
    )
    child = _child(work, body, patch)
    _wait_for(work / "at-queue")  # the child has read `accepted` and not yet taken the guard
    retry(root, body)  # finds accepted, appends, queues, rings
    assert len(peers.take(root, sid)) == 1
    _release(work, "queue")
    assert child.wait(30) == 0
    assert peers.peek(root, sid) == [] and _rings(work) == 1


def test_a_retry_during_a_live_claim_does_not_ring_before_the_reservation_exists(setup, retry):
    root, sid, work = setup
    body = envelope()
    patch = (
        "from agent_cli.ops import peer_wake\n"
        "real_reserve = peer_wake.reserve\n"
        "def reserve(*a, **k):\n    barrier('reserve')\n    return real_reserve(*a, **k)\n"
        "peer_wake.reserve = reserve\n"
    )
    child = _child(work, body, patch)
    _wait_for(work / "at-reserve")
    assert retry(root, body)["wake"] == "in-progress" and _rings(work) == 0
    _release(work, "reserve")
    assert child.wait(30) == 0
    assert _rings(work) == 1


def test_a_late_settle_loses_its_compare_and_set(setup, retry):
    root, sid, work = setup
    body = envelope()
    # The barrier sits after the ring and before the store transaction: blocking inside that
    # transaction would hold SQLite's write lock and stall the retry instead.
    patch = (
        "from agent_cli.ops import peer_wake\n"
        "real_settle = peer_wake.settle\n"
        "def settle(*a, **k):\n    real_settle(*a, **k)\n    barrier('settle')\n"
        "peer_wake.settle = settle\n"
    )
    child = _child(work, body, patch)
    _wait_for(work / "at-settle")  # rang and settled the reservation; the claim is not settled
    later = retry(root, body, at=NOW + timedelta(seconds=store_mod.CLAIM_LEASE_S + 1))
    assert later["wake"] == "coalesced"  # the local reservation the child settled covers it
    _release(work, "settle")
    assert child.wait(30) == 0
    row = store_mod.lookup(store_mod.connect(root), "mac", body["id"])
    assert row is not None and row.state == "wake_coalesced"  # the child's CAS lost
    assert _rings(work) == 1


def test_expiry_during_the_inbox_lock_wait_stops_the_append(setup, retry):
    root, sid, work = setup
    body = envelope()
    late = work / "late"
    reached = work / "waiting"
    patch = (
        f"clock = lambda: NOW + timedelta(minutes=16) if Path({str(late)!r}).exists() else NOW\n"
        "real_guard = receiver.peers_mod.inbox_guard\n"
        "def guard(root, target):\n"
        f"    Path({str(reached)!r}).write_text('1')\n"
        "    return real_guard(root, target)\n"
        "receiver.peers_mod.inbox_guard = guard\n"
    )
    with peers.inbox_guard(root, sid):
        child = _child(work, body, patch)
        _wait_for(reached)
        late.write_text("1")
    assert child.wait(30) == 0
    [out] = [json.loads(p.read_text()) for p in work.glob("out-*.json")]
    assert out["state"] == "accepted" and out["queue"] == "pending"
    assert peers.peek(root, sid) == [] and _rings(work) == 0
