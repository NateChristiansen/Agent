"""`usage_publish`: triggers only enqueue, and a queued request gets a running or scheduled consumer
whenever a process can be launched at all.

Detached launches are replaced by threads running the same `job.run`, so a test sees each run
the job would start. The coordination lock is a real flock or Windows byte lock, which also
excludes between threads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli import frontmatter, notes_sync
from agent_cli.hooks import notes_sync as notes_hook
from agent_cli.usage import collect_day, job, publish

TODAY_REL = "usage/2026/09-September/03-Thursday.mac.md"


class Launches:
    """Stands in for `job.spawn`: each launch runs `job.run` on a thread."""

    def __init__(self, do: Any, fail: int = 0) -> None:
        self.do = do
        self.fail = fail
        self.tokens: list[str] = []
        self.threads: list[threading.Thread] = []
        self.results: list[str] = []

    def __call__(self, cmd: list[str], root: Path) -> SimpleNamespace:
        if self.fail:
            self.fail -= 1
            raise OSError("fork failed")
        token = cmd[-1]
        self.tokens.append(token)
        thread = threading.Thread(target=lambda: self.results.append(job.run(root, token, self.do)))
        self.threads.append(thread)
        thread.start()
        return SimpleNamespace(pid=os.getpid())

    def join(self) -> None:
        while any(t.is_alive() for t in self.threads):
            for thread in list(self.threads):
                thread.join(timeout=20)


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job, "RETRY_FIRST_S", 0.05)
    monkeypatch.setattr(job, "RETRY_MAX_S", 0.1)
    monkeypatch.setattr(job, "POLL_S", 0.01)


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> Any:
    def make(do: Any, fail: int = 0) -> Launches:
        fake = Launches(do, fail)
        monkeypatch.setattr(job, "spawn", fake)
        return fake

    return make


def _ok(root: Path) -> dict[str, Any]:
    return {"status": "pushed", "left": [], "pending": [], "progress": True}


def _outcomes(*items: dict[str, Any]) -> Any:
    rows = iter(items)
    calls: list[int] = []

    def do(root: Path) -> dict[str, Any]:
        calls.append(job.queue(root)["seq"])
        return next(rows, _ok(root))

    do.calls = calls  # type: ignore[attr-defined]
    return do


def _done(root: Path) -> bool:
    queue = job.queue(root)
    return queue["owner"] is None and queue.get("done_seq") == queue["seq"]


def test_a_trigger_starts_one_job_and_a_second_one_only_queues(tmp_root: Path, launches: Any):
    gate = threading.Event()
    running = threading.Event()

    def slow(root: Path) -> dict[str, Any]:
        running.set()
        gate.wait(10)
        return _ok(root)

    fake = launches(slow)
    assert job.request(tmp_root, "stop")["started"] is True
    running.wait(10)
    assert job.request(tmp_root, "session-start")["started"] is False
    gate.set()
    fake.join()
    # The second request was newer than the first run's start, so one fresh run consumed it.
    assert fake.results == ["relaunched", "released"]
    assert _done(tmp_root) and job.queue(tmp_root)["seq"] == 2


def test_a_trigger_that_finds_the_lock_busy_is_recorded_not_lost(tmp_root: Path, launches: Any):
    do = _outcomes()
    fake = launches(do)
    with job.coordination(tmp_root, 1):
        answer = job.request(tmp_root, "stop", wait=0.05)
        spooled = list((tmp_root / job.JOB_DIR / job.SPOOL).glob("*.json"))
        assert answer["started"] == "claimant" and len(spooled) == 1
        assert json.loads(spooled[0].read_text(encoding="utf-8"))["reason"] == "stop"
    fake.join()
    assert fake.tokens == [job.CLAIMANT] and do.calls == [1]
    assert [r["reason"] for r in job.queue(tmp_root)["requests"]] == ["stop"]
    assert _done(tmp_root)
    assert not list((tmp_root / job.JOB_DIR / job.SPOOL).glob("*.json"))


def test_a_request_in_the_final_read_window_is_still_consumed(
    tmp_root: Path, launches: Any, monkeypatch: pytest.MonkeyPatch
):
    at_final = threading.Event()
    go = threading.Event()
    paused: list[str] = []

    def pause(stage: str) -> None:
        if stage == "final-read" and not paused:
            paused.append(stage)
            at_final.set()
            go.wait(10)

    monkeypatch.setattr(job, "pause", pause)
    do = _outcomes()
    fake = launches(do)
    job.request(tmp_root, "stop")
    assert at_final.wait(10)
    # The job has read the queue for the last time and has not released it yet.
    answer = job.request(tmp_root, "dispatch 001-mac-03 done", wait=0.05)
    assert answer["started"] == "claimant"
    go.set()
    fake.join()
    assert do.calls == [1, 2], "the claimant ran a second collection with no later trigger"
    assert fake.results[0] == "released"
    assert _done(tmp_root) and job.queue(tmp_root)["seq"] == 2


def test_owed_work_relaunches_after_progress_and_waits_without_it(tmp_root: Path, launches: Any):
    owed = {"status": "pushed", "left": ["2026-09-03"], "pending": ["1 day(s) not reached"]}
    do = _outcomes({**owed, "progress": True}, {**owed, "progress": False})
    fake = launches(do)
    job.request(tmp_root, "stop")
    fake.join()
    # Progress hands over to a fresh run; no progress keeps that run as the consumer, which
    # waits and tries again until nothing is owed.
    assert fake.results == ["relaunched", "released"]
    assert len(do.calls) == 3 and _done(tmp_root)


def test_a_partial_source_with_no_progress_keeps_a_consumer(tmp_root: Path, launches: Any):
    partial = {"status": "pushed", "left": [], "pending": ["claude partial at 2026-09-01"], "progress": False}
    do = _outcomes(partial, partial)
    fake = launches(do)
    job.request(tmp_root, "stop")
    fake.join()
    assert fake.results == ["released"] and len(do.calls) == 3
    assert _done(tmp_root)


def test_settled_unavailable_evidence_marks_the_request_done(tmp_root: Path, launches: Any):
    settled = {"status": "pushed", "pending": [], "settled": ["codex unavailable at x"], "progress": False}
    do = _outcomes(settled)
    fake = launches(do)
    job.request(tmp_root, "stop")
    fake.join()
    assert fake.results == ["released"] and len(do.calls) == 1 and _done(tmp_root)


def test_a_failure_is_kept_and_retried(tmp_root: Path, launches: Any):
    do = _outcomes({"status": "offline", "detail": "fetch failed"})
    fake = launches(do)
    job.request(tmp_root, "stop")
    fake.join()
    failures = job.state(tmp_root)["failures"]
    assert failures[0]["stage"] == "publish" and "offline" in failures[0]["detail"]
    assert fake.results == ["released"] and len(do.calls) == 2 and _done(tmp_root)


def test_a_failed_launch_is_retried(tmp_root: Path, launches: Any):
    do = _outcomes()
    fake = launches(do, fail=1)
    assert job.request(tmp_root, "stop")["started"] is True
    fake.join()
    assert do.calls == [1] and _done(tmp_root)
    assert job.state(tmp_root)["failures"][0]["stage"] == "spawn"


def test_a_failed_hand_off_keeps_the_queue_and_runs_again_itself(tmp_root: Path, launches: Any):
    gate = threading.Event()
    running = threading.Event()

    def slow(root: Path) -> dict[str, Any]:
        running.set()
        gate.wait(10)
        return _ok(root)

    fake = launches(slow)
    job.request(tmp_root, "stop")
    running.wait(10)
    fake.fail = 100
    assert job.request(tmp_root, "dispatch done")["started"] is False
    gate.set()
    fake.join()
    assert fake.results == ["released"] and _done(tmp_root) and job.queue(tmp_root)["seq"] == 2


def test_a_claimant_outwaits_a_busy_lock(tmp_root: Path, launches: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "JOB_WAIT_S", 0.03)
    do = _outcomes()
    fake = launches(do)
    with job.coordination(tmp_root, 1):
        assert job.request(tmp_root, "stop", wait=0.01)["started"] == "claimant"
        time.sleep(0.3)
    fake.join()
    assert fake.results == ["released"] and do.calls == [1] and _done(tmp_root)
    assert any(f["stage"] == "lock" for f in job.state(tmp_root)["failures"])


def _owner(root: Path, pid: int, started: str) -> None:
    data = job.queue(root)
    data["owner"] = {
        "token": "gone",
        "pid": pid,
        "started": started,
        "until": time.time() + 600,
        "since": time.time(),
    }
    (root / job.JOB_DIR).mkdir(parents=True, exist_ok=True)
    (root / job.JOB_DIR / job.QUEUE).write_text(json.dumps(data), encoding="utf-8")


def test_a_dead_owner_does_not_hold_the_queue(tmp_root: Path, launches: Any):
    fake = launches(_outcomes())
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    _owner(tmp_root, gone.pid, "")
    assert job.request(tmp_root, "stop")["started"] is True
    fake.join()
    assert _done(tmp_root)


def test_a_reused_pid_does_not_hold_the_queue(tmp_root: Path, launches: Any, monkeypatch: pytest.MonkeyPatch):
    fake = launches(_outcomes())
    # This pid is alive, but its start time is not the one the owner recorded.
    _owner(tmp_root, os.getpid(), "Mon Jan  1 00:00:00 2001")
    # The start-time comparison is the contract. Sandboxes may deny the native ps read.
    monkeypatch.setattr(job, "identity", lambda pid, timeout=0.0: "Tue Feb  2 00:00:00 2002")
    assert job.request(tmp_root, "stop")["started"] is True
    fake.join()
    assert _done(tmp_root)


def test_a_live_owner_holds_the_queue(tmp_root: Path, launches: Any):
    fake = launches(_outcomes())
    _owner(tmp_root, os.getpid(), job.identity(os.getpid()))
    assert job.request(tmp_root, "stop")["started"] is False
    assert fake.tokens == []


def test_stop_enqueues_even_when_notes_sync_raises(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    def broken(root: Path, record: object) -> notes_sync.Result:
        raise OSError("disk gone")

    seen: list[str] = []
    monkeypatch.setattr(notes_sync, "sync", broken)
    monkeypatch.setattr(job, "trigger_quietly", lambda root, reason: seen.append(reason))
    assert notes_hook.notes_sync_hook(tmp_root, {"hook_event_name": "Stop", "session_id": ""}) == ""
    assert seen == ["stop"]


def test_an_owner_without_a_start_time_holds_the_queue_only_for_its_lease(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    launched: list[str] = []
    monkeypatch.setattr(
        job, "spawn", lambda cmd, root: launched.append(cmd[-1]) or SimpleNamespace(pid=os.getpid())
    )
    monkeypatch.setattr(job, "identity", lambda pid, timeout=0.0: "")
    assert job.request(tmp_root, "stop")["started"] is True
    # That run never claimed the queue, its pid now names a live process, and no start time
    # could be read: only the lease keeps it the owner, and the lease has run out.
    real = time.time
    monkeypatch.setattr(job.time, "time", lambda: real() + 120)
    assert job.request(tmp_root, "stop")["started"] is True
    assert len(launched) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="the slow start-time read here is a slow `ps`")
def test_a_trigger_stays_within_its_wall_time(
    tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    slow = tmp_path / "slow-bin"
    slow.mkdir()
    (slow / "ps").write_text("#!/bin/sh\nsleep 5\necho 'Mon Jan  1 00:00:00 2001'\n", encoding="utf-8")
    (slow / "ps").chmod(0o755)
    monkeypatch.setenv("PATH", f"{slow}{os.pathsep}{os.environ['PATH']}")

    def failing(cmd: list[str], root: Path) -> SimpleNamespace:
        raise OSError("fork failed")

    def timed(label: str) -> None:
        began = time.monotonic()
        job.request(tmp_root, label)
        took[label] = round(time.monotonic() - began, 3)

    def clear() -> None:
        data = job.queue(tmp_root)
        data["owner"] = None
        (tmp_root / job.JOB_DIR / job.QUEUE).write_text(json.dumps(data), encoding="utf-8")

    took: dict[str, float] = {}
    monkeypatch.setattr(job, "spawn", failing)
    _owner(tmp_root, os.getpid(), "Tue Feb  2 00:00:00 2002")
    timed("owner start time read slowly")
    clear()
    timed("three failed launches")
    with job.coordination(tmp_root, 1):
        timed("busy lock, then three failed launches")
    monkeypatch.setattr(job, "spawn", lambda cmd, root: SimpleNamespace(pid=os.getpid()))
    clear()
    timed("launch, then its start time read slowly")
    print(f"trigger wall times: {took}")
    assert max(took.values()) <= 2.0, took


def test_hooks_start_nothing_under_the_test_clock(tmp_root: Path, launches: Any):
    fake = launches(_ok)
    job.trigger_quietly(tmp_root, "stop")
    assert fake.tokens == [] and not (tmp_root / job.JOB_DIR / job.QUEUE).exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod(0) does not make a directory unreadable on Windows"
)
def test_an_unavailable_source_is_retried_until_its_evidence_is_unchanged(
    tmp_root: Path, notes_remote: Path, tmp_path: Path, launches: Any
):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\nnotes_sync: pull\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude" / "projects").chmod(0)
    outcomes: list[dict[str, Any]] = []

    def do(root: Path) -> dict[str, Any]:
        outcome = publish.publish(root, collect=lambda r, d, t: collect_day(r, d, machine=t, home=home))
        outcomes.append(outcome.as_dict())
        return outcome.as_dict()

    fake = launches(do)
    try:
        job.request(tmp_root, "stop")
        fake.join()
    finally:
        (home / ".claude" / "projects").chmod(0o755)
    # The first pass found new unavailable evidence, so it was not done; the second found it
    # unchanged, settled it, and only then marked the request done.
    assert fake.results == ["relaunched", "released"] and _done(tmp_root)
    assert [o["pending"] for o in outcomes] == [["claude unavailable at 2026-08-04T00:00:00" + _offset()], []]
    assert outcomes[1]["settled"] == ["claude unavailable at 2026-08-04T00:00:00" + _offset()]


def _offset() -> str:
    from datetime import datetime

    return datetime(2026, 8, 4).astimezone().isoformat()[19:]


def test_a_completion_during_collection_is_published_with_no_later_turn(
    tmp_root: Path, notes_remote: Path, tmp_path: Path, launches: Any
):
    (tmp_root / "agent.local.yaml").write_text("machine: mac\nnotes_sync: on\n", encoding="utf-8")
    home = tmp_path / "home"
    collected = threading.Event()
    finish = threading.Event()

    def gather(root: Path, day: date, tag: str) -> dict[str, Any]:
        record = collect_day(root, day, machine=tag, home=home)
        if day == date(2026, 9, 3) and not collected.is_set():
            collected.set()
            finish.wait(10)
        return record

    fake = launches(lambda root: publish.publish(root, collect=gather).as_dict())
    job.request(tmp_root, "session-start")
    assert collected.wait(20)
    record = {
        "id": "001-mac-03",
        "kind": "work",
        "row": "build",
        "provider": "claude",
        "attempt": 1,
        "cost_usd": 1.5,
        "last_result": {"type": "result", "total_cost_usd": 1.5},
        "finished": "2026-09-03T14:00:00-07:00",
    }
    path = tmp_root / ".agent" / "dispatches" / "001-mac-03.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert job.request(tmp_root, "dispatch 001-mac-03 done")["started"] is False
    finish.set()
    fake.join()
    assert fake.results == ["relaunched", "released"]
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=notes_remote, check=True)
    shown = subprocess.run(
        ["git", "show", f"origin/main:{TODAY_REL}"],
        cwd=notes_remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    meta, _ = frontmatter.split(shown)
    assert meta["dispatches"] == 1 and meta["dispatch_usd"] == 1.5


# Audit 10 plan.md:116 (K4): Stop uses trigger_quietly, not the unused stop_due helper.


def _failing(root: Path) -> dict[str, Any]:
    return {"status": "error", "detail": "offline"}


def test_the_suite_starts_no_detached_job(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    """Two `agent_cli.usage.job` processes outlived a pytest run and
    held venv DLLs. The suite sets `AGENT_NO_USAGE` for itself and every child; with the fixed
    clock cleared a trigger still enqueues nothing, and the real `spawn` refuses."""
    assert os.environ.get(job.OFF_ENV)
    monkeypatch.delenv("AGENT_NOW", raising=False)
    job.trigger_quietly(tmp_root, "session-start")
    assert not (tmp_root / job.JOB_DIR / job.QUEUE).exists()
    assert job.request(tmp_root, "stop")["started"] is False
    assert all(job.OFF_ENV in f["detail"] for f in job.state(tmp_root)["failures"])
    with pytest.raises(OSError, match=job.OFF_ENV):
        job.spawn([sys.executable, "-c", "pass"], tmp_root)


def _remove_tree(root: Path) -> None:
    """`shutil.rmtree` that also removes read-only files and a directory that is the cwd, which
    Windows refuses (git objects; the `tmp_root` fixture chdirs into the root)."""
    if sys.platform != "win32":
        shutil.rmtree(root)
        return
    os.chdir(root.parent)

    def writable(func: Any, path: str, _error: BaseException) -> None:
        os.chmod(path, 0o700)
        func(path)

    shutil.rmtree(root, onexc=writable)


def test_a_root_deleted_during_publication_is_not_recreated(tmp_root: Path, launches: Any):
    """The root removed inside the publication pass. The job
    exits without its post-publication writes (`note`, `_fold`) recreating the root."""

    def delete(root: Path) -> dict[str, Any]:
        _remove_tree(root)
        return _ok(root)

    fake = launches(delete)
    job.request(tmp_root, "stop")
    fake.join()
    assert fake.results == ["root-gone"]
    assert not tmp_root.exists()
    job.note(tmp_root, last={"status": "pushed"})
    with pytest.raises(FileNotFoundError):
        job._fold(tmp_root, {})  # pyright: ignore[reportPrivateUsage]
    assert not tmp_root.exists()


def test_a_job_whose_root_is_deleted_exits_without_recreating_it(
    tmp_root: Path, launches: Any, monkeypatch: pytest.MonkeyPatch
):
    real_wait = job._wait  # pyright: ignore[reportPrivateUsage]

    def wait_then_delete(root: Path, start_seq: int, seconds: float) -> None:
        _remove_tree(root)
        real_wait(root, start_seq, seconds)

    monkeypatch.setattr(job, "_wait", wait_then_delete)
    fake = launches(_failing)
    job.request(tmp_root, "stop")
    fake.join()
    assert fake.results == ["root-gone"]
    assert not tmp_root.exists()
    with pytest.raises(FileNotFoundError), job.coordination(tmp_root, 0.1):
        pass
    assert not tmp_root.exists()


def test_a_job_past_its_life_releases_the_queue(
    tmp_root: Path, launches: Any, monkeypatch: pytest.MonkeyPatch
):
    """A job still waiting at `JOB_MAX_S` exits; its request is not lost, the next trigger starts
    a fresh job for it."""
    monkeypatch.setattr(job, "JOB_MAX_S", 0.0)
    fake = launches(_failing)
    job.request(tmp_root, "stop")
    fake.join()
    assert fake.results == ["expired"]
    data = job.queue(tmp_root)
    assert data["owner"] is None and data.get("done_seq") != data["seq"]
    assert job.request(tmp_root, "session-start")["started"] is True
    fake.join()
