"""the verification lane `verify:<short>`. Supervisor verification waits for it
with no short guard held and a bounded wait; reverify never waits; a dead holder keeps it until
its verifier is gone; the records guard is back to K0's 3 s deadline."""

from __future__ import annotations

import os
import shlex
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import dispatch, leases, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import holdings, lane, lifecycle, recovery, telemetry_events, verification
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load, save, verification_summary
from agent_cli.telemetry import schema
from dispatch_helpers import DEAD_PID, _new, _set_alpha_dispatch, droot, envelope, one  # noqa: F401
from test_dispatch_reverify import _blocked_by_dirt
from test_dispatch_verification import finish, ready, wait_for  # noqa: F401

LANE = "verify:alpha"


class Clock:
    """`lane._clock` and `lane._sleep`: each sleep moves time on a minute and runs `on_sleep`."""

    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: Callable[[float], object] = lambda now: None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += 60.0
        self.on_sleep(self.now)


def _clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(lane, "_clock", clock)
    monkeypatch.setattr(lane, "_sleep", clock.sleep)
    return clock


def _finished(root: Path) -> dict:
    """A second launched dispatch whose worker returned done (`ready` stubbed the start)."""
    d = _new(root)
    ret = envelope()
    ret["session_id"], ret["structured_output"]["nonce"] = d["session_id"], d["nonce"]
    d.update(state="running", supervisor_pid=DEAD_PID, stop_reason="result", exit_code=0, last_result=ret)
    d["worker_cleanup_pending"] = False
    save(root, d)
    return d


def _paused_verifier(root: Path) -> tuple[Path, Path]:
    marker, release = root / "verifier-started", root / "verifier-release"
    command = (
        f"touch {shlex.quote(str(marker))}; while ! test -f {shlex.quote(str(release))}; do sleep .02; done"
    )
    _set_alpha_dispatch(root, verify=[{"command": command, "timeout_seconds": 10}], verify_timeout_seconds=12)
    return marker, release


def _held_by(root: Path, dispatch_id: str) -> list[str]:
    return sorted(
        str(x["area"]) for x in leases.list_all(root) if x.get("session_id") == f"dispatch:{dispatch_id}"
    )


def test_a_second_verifier_waits_unguarded_and_can_be_read_written_and_cancelled(ready):
    root, first = ready
    _set_alpha_dispatch(root, max_concurrent=2)
    second = _finished(root)
    marker, release = _paused_verifier(root)
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(gate.execute, root, first["id"], 1)
        wait_for(marker.exists)
        waiting = pool.submit(gate.execute, root, second["id"], 1)
        wait_for(lambda: (leases.get(root, LANE) or {}).get("session_id") == f"dispatch:{first['id']}")
        time.sleep(0.3)
        assert not waiting.done() and load(root, second["id"]).get("verification") is None
        assert len(_held_by(root, second["id"])) == 2
        started = time.monotonic()
        with gate.record_guard(root, second["id"], timeout=1.0) as current:
            current["note_during_wait"] = True
            save(root, current)
        assert time.monotonic() - started < 1.0 and load(root, second["id"])["note_during_wait"]
        cancelled = recovery.cancel(root, dispatch_id=second["id"])
        waiting.result(timeout=10)
        release.touch()
        running.result(timeout=15)
    record = load(root, second["id"])
    assert cancelled["state"] == "cancelled" and record["terminal_committed"]
    assert record.get("verification") is None and verification_summary(record)["status"] != "verified"
    assert leases.get(root, LANE) is None and _held_by(root, second["id"]) == []


def test_the_wait_is_counted_apart_from_the_command(ready, monkeypatch):
    root, d = ready
    leases.take_marked(root, LANE, session_id="someone", pid=os.getpid())
    clock = _clock(monkeypatch)
    clock.on_sleep = lambda now: now >= 300 and leases.release(root, LANE, "someone")
    gate.execute(root, d["id"], 1)
    record = load(root, d["id"])
    assert record["verification_wait"]["verify_wait_s"] == 300.0
    assert record["verification_wait"]["concurrent_workers"] == 1
    assert record["verification_telemetry"]["duration_s"] < 60
    assert record["run_area"] == lane.run_area(gate.metadata(record)[0])  # type: ignore[index]
    assert finish(root, record)["state"] == "done" and leases.get(root, LANE) is None


def test_a_lane_timeout_records_incomplete_and_starts_no_verifier(ready, monkeypatch):
    root, d = ready
    _set_alpha_dispatch(root, verify_wait_timeout_s=600)
    leases.take_marked(root, LANE, session_id="someone", pid=os.getpid())
    _clock(monkeypatch)
    monkeypatch.setattr(lane, "prepare", lambda *a, **k: pytest.fail("a verifier was prepared"))
    gate.execute(root, d["id"], 1)
    record = load(root, d["id"])
    assert record.get("verification") is None and record["verification_wait"]["outcome"] == "timeout"
    assert record["verification_result"]["status"] == "incomplete"
    assert "no verification lane within 600 s" in record["verification_result"]["reasons"][0]
    assert "verify:alpha is held by someone" in record["verification_result"]["reasons"][0]
    done = finish(root, record)
    assert done["state"] == "blocked" and done["verification_result"]["status"] == "incomplete"


def test_a_cancel_before_the_lane_records_incomplete(ready, monkeypatch):
    root, d = ready
    leases.take_marked(root, LANE, session_id="someone", pid=os.getpid())
    clock = _clock(monkeypatch)

    def cancel(now: float) -> None:
        with gate.record_guard(root, d["id"]) as current:
            current["state"] = "cancelled"
            save(root, current)

    clock.on_sleep = cancel
    gate.execute(root, d["id"], 1)
    record = load(root, d["id"])
    assert record.get("verification") is None and record["verification_wait"]["outcome"] == "cancelled"
    assert record["verification_result"] == {
        "status": "incomplete",
        "reasons": ["cancelled before the verifier started"],
    }


def test_a_cancel_that_races_the_take_is_caught_by_the_recheck(ready, monkeypatch):
    root, d = ready
    real = lane.take_now

    def take_then_cancel(*args, **kwargs):
        taken = real(*args, **kwargs)
        with gate.record_guard(root, d["id"]) as current:
            current["state"] = "cancelled"
            save(root, current)
        return taken

    monkeypatch.setattr(lane, "take_now", take_then_cancel)
    monkeypatch.setattr(lane, "prepare", lambda *a, **k: pytest.fail("a verifier was prepared"))
    gate.execute(root, d["id"], 1)
    record = load(root, d["id"])
    assert record.get("verification") is None and record["verification_wait"]["outcome"] == "cancelled"
    assert leases.get(root, LANE) is None


def test_a_crashed_holder_with_a_live_verifier_keeps_the_lane(ready, monkeypatch):
    root, d = ready
    gate.execute(root, d["id"], 1)
    leases.take_marked(root, LANE, session_id=f"dispatch:{d['id']}", pid=DEAD_PID)
    monkeypatch.setattr(
        verification, "load_status", lambda root, binding: SimpleNamespace(requires_lease=True)
    )
    assert leases.status(leases.get(root, LANE), root) == "live"
    with pytest.raises(AgentError, match="is held by"):
        leases.take(root, LANE, session_id="peer", pid=os.getpid())
    assert holdings.reconcile(root, limit=8) == []
    monkeypatch.undo()
    assert holdings.reconcile(root, limit=8) == [LANE]


def test_reverify_beside_a_running_verifier_is_refused_at_once(ready):
    root, d = ready
    blocked = _blocked_by_dirt(root, d)
    leases.take_marked(root, LANE, session_id="dispatch:001-mac-09", pid=os.getpid())
    started = time.monotonic()
    with pytest.raises(lane.LaneBusy, match="lane busy: verify:alpha"):
        reverify_mod.reverify(root, d["id"])
    assert time.monotonic() - started < 5
    after = load(root, d["id"])
    assert after["record_revision"] == blocked["record_revision"] and not after.get("reverify_runs")
    assert _held_by(root, d["id"]) == []


def test_an_1800_second_wait_trips_neither_the_dispatch_timeout_nor_a_watchdog(
    droot: Path, fake_claude, monkeypatch
):
    _set_alpha_dispatch(droot, timeout_minutes=1)
    leases.take_marked(droot, LANE, session_id="someone", pid=os.getpid())
    clock = _clock(monkeypatch)
    clock.on_sleep = lambda now: now >= 1799 and leases.release(droot, LANE, "someone")
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    d = _new(droot)
    assert d["state"] == "done" and d["verification_result"]["status"] == "verified"
    assert d["timed_out"] is False and d["stop_reason"] == "result"
    assert d["verification_wait"]["verify_wait_s"] >= 1799


def test_a_record_write_during_reverify_waits_instead_of_failing(ready, monkeypatch):
    root, d = ready
    _blocked_by_dirt(root, d)
    real = reverify_mod._git
    adding = threading.Event()

    def slow_add(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            adding.set()
            time.sleep(4)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(reverify_mod, "_git", slow_add)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(reverify_mod.reverify, root, d["id"])
        assert adding.wait(10)
        for _ in range(3):
            dispatch.show(root, dispatch_id=d["id"])
            with gate.record_guard(root, d["id"]) as current:
                current["written_during_reverify"] = True
                save(root, current)
            time.sleep(0.5)
        row = future.result(timeout=30)
    assert row["result"]["status"] == "verified" and load(root, d["id"])["written_during_reverify"]


def test_the_lane_and_wait_settings_are_validated():
    from agent_cli.repos_validate import validate_dispatch

    assert "dispatch.max_concurrent_verify must be a positive integer" in validate_dispatch(
        {"max_concurrent_verify": 0}
    )
    assert "dispatch.verify_wait_timeout_s must be a positive number of seconds" in validate_dispatch(
        {"verify_wait_timeout_s": -1}
    )
    assert validate_dispatch({"max_concurrent_verify": 2, "verify_wait_timeout_s": 90.5}) == []


def test_the_telemetry_fields_pass_the_t2_validator():
    d = {"attempt": 2, "verification_wait": {"attempt": 2, "verify_wait_s": 12.5, "concurrent_workers": 2}}
    fields = telemetry_events._lane_wait(d)
    assert fields == {"verify_wait_s": 12.5, "concurrent_workers": 2}
    assert schema._data({"run_id": None, "attempt": 2, **fields}) is None
    assert telemetry_events._lane_wait({**d, "attempt": 3}) == {}


def test_the_default_caps_keep_one_lane_and_the_1800_second_wait(droot: Path):
    rp = repos.load_registry(droot).require("alpha")
    assert lane.lanes_of(rp) == 1 and lane.wait_limit(rp) == 1800.0
    assert lane.lane_area("alpha", 0) == LANE and lane.lane_area("alpha", 1) == "verify:alpha/1"
    assert lifecycle.holdings.cap_of(rp) == 3
