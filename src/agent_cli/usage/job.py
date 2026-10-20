"""`usage_publish`: one detached job per machine that keeps this machine's usage files current.

Triggers (Stop, dispatch completion, session start) only enqueue, and start a job when no live
job owns the queue. They never collect. Two short critical sections hold the coordination lock
under `.agent/usage/`: a trigger's append-and-start decision, and a job's final pending read
followed by a fresh run, another pass, or the release of ownership. So `owner` set under the
lock means a job that has not yet made its final read, and a request appended while it is set
is seen by that read. An owner is live while its lease has not run out, unless its pid is dead
or now belongs to a process with another start time. A running job renews its lease every
`HEARTBEAT_S`, so the lease is what proves it; when a start time cannot be read the pid proves
nothing either way, and a job whose pass runs past `PASS_MAX_S` stops renewing.

A trigger spends at most `TRIGGER_BUDGET_S` in all: the lock wait, each process-start read and
each launch try take their share of it, and what does not fit is skipped. A trigger that cannot
get the lock in `TRIGGER_WAIT_S` writes its request to `spool/` and starts
a claimant, which waits for the lock as long as it takes, folds the spool into the queue, and
runs only when no live job owns it. A launch that fails is retried `SPAWN_TRIES` times; a job
whose hand-off launch fails keeps the queue and runs the next pass itself.

A job remembers the queue position it started from. At shutdown a newer request, or work still
owed after a pass that made progress, hands ownership to one fresh detached run. Work still owed
after a pass with no progress keeps the job as the consumer: it waits, with backoff, and wakes
early for a new request. The queue is marked done only when nothing is owed; an `unavailable`
source whose evidence is unchanged is settled and owes nothing (`publish.owed`). A job killed
outright, or a trigger whose every launch failed, leaves its requests to the next trigger.

A job's life is bounded (two jobs a finished pytest run left behind still mapped venv DLLs, so `uv sync` could not replace them). It exits when its root is gone,
and a job still waiting after `JOB_MAX_S` releases the queue and exits; the requests it leaves
start a fresh job at the next trigger. With `AGENT_NO_USAGE` set (the test suite sets it for
every process it starts) no trigger enqueues and `spawn` starts nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

from .. import core
from ..records.narrow import as_dict, as_float, as_int, dict_list

JOB_DIR = core.agent_path(Path(), "usage")
QUEUE = "pending.json"
STATE = "state.json"
SPOOL = "spool"
LOCK = "coord.lock"
TRIGGER_BUDGET_S = 1.8
TRIGGER_WAIT_S = 0.8
TRIGGER_IDENTITY_S = 0.25
JOB_IDENTITY_S = 5.0
JOB_WAIT_S = 60.0
LEASE_S = 60.0
HEARTBEAT_S = 15.0
PASS_MAX_S = 600.0
JOB_MAX_S = 3 * 3600.0
OFF_ENV = "AGENT_NO_USAGE"
SPAWN_TRIES = 3
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 900.0
POLL_S = 2.0
KEEP_REQUESTS = 50
KEEP_FAILURES = 20
CLAIMANT = "-"  # the token argument of a run started without ownership, after a busy lock
OK = ("written", "committed", "current", "pushed")


def job_dir(root: Path) -> Path:
    return root / JOB_DIR


@contextlib.contextmanager
def coordination(root: Path, wait: float) -> Generator[None]:
    """The short coordination lock. TimeoutError when it stays busy past `wait`; FileNotFoundError
    when `root` is gone, rather than recreating a deleted root for a job that outlived it."""
    if not root.is_dir():
        raise FileNotFoundError(f"usage root {root} is gone")
    job_dir(root).mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from .. import win_lock

        fd = win_lock.open_lock_file(root, (*JOB_DIR.parts, LOCK))
        try:
            with win_lock.exclusive(fd, timeout=wait):
                yield
        finally:
            os.close(fd)
        return
    import fcntl

    with (job_dir(root) / LOCK).open("a") as fh:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("usage coordination lock is busy") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, Any]:
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}


def _save(path: Path, data: dict[str, Any]) -> None:
    core.atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def queue(root: Path) -> dict[str, Any]:
    return _read(job_dir(root) / QUEUE)


def state(root: Path) -> dict[str, Any]:
    return _read(job_dir(root) / STATE)


def _fold(root: Path, data: dict[str, Any], reason: str = "") -> int:
    """Number spooled requests, and `reason` when given, into the queue. Returns the last seq.
    FileNotFoundError when `root` is gone: the write would recreate it."""
    if not root.is_dir():
        raise FileNotFoundError(f"usage root {root} is gone")
    seq = as_int(data.get("seq"))
    requests = dict_list(data.get("requests"))
    spooled = sorted((job_dir(root) / SPOOL).glob("*.json"))
    for path in spooled:
        entry = _read(path)
        seq += 1
        requests.append({"seq": seq, "reason": str(entry.get("reason") or "spooled"), "at": entry.get("at")})
    if reason:
        seq += 1
        requests.append({"seq": seq, "reason": reason, "at": core.iso(core.now())})
    data["seq"] = seq
    data["requests"] = requests[-KEEP_REQUESTS:]
    _save(job_dir(root) / QUEUE, data)
    for path in spooled:
        path.unlink(missing_ok=True)
    return seq


def identity(pid: int, timeout: float = JOB_IDENTITY_S) -> str:
    """A process's start time, which with its pid tells it from a later process given the same
    number. Empty when it cannot be read within `timeout`: no such process, or a query that
    failed or ran slow, so an empty value proves nothing about the pid."""
    if sys.platform == "win32":
        from ..winproc import pid_started

        return pid_started(pid)
    if timeout <= 0:
        return ""
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    return done.stdout.strip()


def _owner(token: str, pid: int | None, timeout: float = JOB_IDENTITY_S, **extra: Any) -> dict[str, Any]:
    return {
        "token": token,
        "pid": pid,
        "started": identity(pid, timeout) if pid else "",
        "until": time.time() + LEASE_S,
        **extra,
    }


def _live(owner: object, timeout: float = JOB_IDENTITY_S) -> bool:
    """The lease decides; a dead pid, or a live pid with another start time, ends it early."""
    record = as_dict(owner)
    if not record.get("token") or time.time() > as_float(record.get("until")):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int):
        return True
    if not core.is_alive(pid):
        return False
    started = record.get("started")
    now = identity(pid, timeout) if started else ""
    return not (started and now and now != started)


def note(root: Path, **fields: Any) -> None:
    """Merge a run's outcome, or append a failure, into `state.json`, where later runs keep
    adding to it. The queue itself is `pending.json`. Nothing when `root` is gone: the write
    would recreate it."""
    if not root.is_dir():
        return
    current = state(root)
    if "failure" in fields:
        failures = [*dict_list(current.get("failures")), {"at": core.iso(core.now()), **fields["failure"]}]
        current["failures"] = failures[-KEEP_FAILURES:]
        fields.pop("failure")
    current.update(fields)
    _save(job_dir(root) / STATE, current)


def argv(root: Path, token: str) -> list[str]:
    return [sys.executable, "-m", "agent_cli.usage.job", str(root), token]


def spawn(cmd: list[str], root: Path) -> Any:
    """Start a run that outlives the hook that asked for it, with no console and no stdio. Refused
    under `AGENT_NO_USAGE`, so a test that reaches a real trigger starts no detached job."""
    from ..launcher import spawn_supervisor

    if os.environ.get(OFF_ENV):
        raise OSError(f"{OFF_ENV} is set: no detached usage job")

    # The end hook's child becomes an orphan as the hook exits. It must not inherit the
    # manager or dispatch ownership tag, or that owner's reaper can kill the final flush.
    from ..proc_tags import TAG_DISPATCH, TAG_SESSION

    env = {key: value for key, value in os.environ.items() if key.upper() not in (TAG_DISPATCH, TAG_SESSION)}
    proc, _ = spawn_supervisor(
        subprocess.Popen,
        cmd,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return proc


class Budget:
    """What is left of a fixed wall-time allowance; unlimited when `seconds` is None."""

    def __init__(self, seconds: float | None = None) -> None:
        self.end = None if seconds is None else time.monotonic() + seconds

    def left(self, cap: float) -> float:
        return cap if self.end is None else max(0.0, min(cap, self.end - time.monotonic()))


def _launch(root: Path, token: str, budget: Budget) -> Any | None:
    """`spawn`, tried `SPAWN_TRIES` times while the budget lasts; None, with each failure noted,
    when no try worked."""
    for attempt in range(SPAWN_TRIES):
        if attempt and budget.left(1.0) <= 0:
            break
        try:
            return spawn(argv(root, token), root)
        except OSError as exc:
            note(root, failure={"stage": "spawn", "detail": f"try {attempt + 1}: {exc}"})
            time.sleep(budget.left(0.05 * (attempt + 1)))
    return None


def _own(root: Path, data: dict[str, Any], budget: Budget, identity_s: float = JOB_IDENTITY_S) -> bool:
    """Give the queue to a fresh run and start it; False, with ownership released, if it cannot."""
    token = uuid.uuid4().hex
    data["owner"] = _owner(token, None)
    _save(job_dir(root) / QUEUE, data)
    proc = _launch(root, token, budget)
    if proc is None:
        data["owner"] = None
        _save(job_dir(root) / QUEUE, data)
        return False
    pid = getattr(proc, "pid", None)
    data["owner"] = _owner(token, pid if isinstance(pid, int) else None, budget.left(identity_s))
    _save(job_dir(root) / QUEUE, data)
    return True


def request(root: Path, reason: str, *, wait: float = TRIGGER_WAIT_S) -> dict[str, Any]:
    budget = Budget(TRIGGER_BUDGET_S)
    try:
        with coordination(root, budget.left(wait)):
            data = queue(root)
            seq = _fold(root, data, reason)
            if _live(data.get("owner"), budget.left(TRIGGER_IDENTITY_S)):
                return {"queued": seq, "started": False}
            return {"queued": seq, "started": _own(root, data, budget, TRIGGER_IDENTITY_S)}
    except TimeoutError:
        spool = job_dir(root) / SPOOL / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
        core.atomic_write(spool, json.dumps({"reason": reason, "at": core.iso(core.now())}) + "\n")
        started = _launch(root, CLAIMANT, budget) is not None
        return {"queued": None, "spooled": spool.name, "started": "claimant" if started else False}


#: Collect locally on every trigger, but publish notes at most once per 15 minutes to bound commit
#: churn. Flush at session end; failed publication keeps its request and does not advance the mark.
STOP_INTERVAL_S = 15 * 60
STOP_MARK = "stop-trigger"


def trigger_quietly(root: Path, reason: str) -> None:
    """The hook entry: never raises, and starts nothing under the test clock."""
    if os.environ.get("AGENT_NOW") or os.environ.get(OFF_ENV):
        return
    try:
        request(root, reason)
    except Exception:  # noqa: BLE001, S110 — a hook never fails on usage publication
        pass


def mark_publication(root: Path, now: float) -> None:
    """Renew the shared cadence after a successful publish, including a forced final flush."""
    mark = job_dir(root) / STOP_MARK
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.touch()
    os.utime(mark, (now, now))


def pause(stage: str) -> None:
    """A seam the tests replace to hold a run at `stage`; does nothing in a real run."""


def work(root: Path) -> dict[str, Any]:
    from ..dispatch.machine import resolve
    from ..telemetry.dashboard import capture
    from ..telemetry.summary import summary_rel
    from ..vault.live import write_quietly
    from .collect import collect_day
    from .files import record_rel, render
    from .publish import publish

    tag = resolve(root).tag
    if tag is None:
        return {"status": "no-label", "pending": ["no machine label"]}
    day = core.today()
    record = collect_day(root, day, machine=tag)
    local = job_dir(root)
    core.atomic_write(local / record_rel(day, tag).removeprefix("usage/"), render(record))
    core.atomic_write(
        local / summary_rel(day, tag).removeprefix("usage/"),
        json.dumps(capture(root, day, record), indent=2) + "\n",
    )
    write_quietly(root)
    data = queue(root)
    force = any(
        r.get("reason") == "session-end" and as_int(r.get("seq")) > as_int(data.get("done_seq"))
        for r in dict_list(data.get("requests"))
    )
    mark = job_dir(root) / STOP_MARK
    try:
        due = time.time() - mark.stat().st_mtime >= STOP_INTERVAL_S
    except OSError:
        due = True
    if not force and not due:
        return {
            "status": "written",
            "pending": [],
            "left": [],
            "progress": False,
            "detail": "local records written; notes cadence not due",
        }
    outcome = publish(root).as_dict()
    if outcome["status"] in OK:
        mark_publication(root, time.time())
        # Open-day partial sources are observed coverage, not a reason to publish repeatedly.
        outcome["pending"] = [p for p in outcome["pending"] if day.isoformat() not in p]
    return outcome


def _locked[T](root: Path, body: Callable[[], T]) -> T:
    """`body` under the coordination lock, however long the lock stays busy: a detached run is
    the consumer, so it waits rather than leave the queue without one."""
    while True:
        try:
            with coordination(root, JOB_WAIT_S):
                return body()
        except TimeoutError:
            note(root, failure={"stage": "lock", "detail": f"coordination lock busy over {JOB_WAIT_S:.0f}s"})


def _start(root: Path, token: str) -> tuple[str, int] | None:
    def body() -> tuple[str, int] | None:
        data = queue(root)
        seq = _fold(root, data)
        owner = as_dict(data.get("owner"))
        mine = token
        if mine == CLAIMANT:
            if _live(owner):
                return None
            mine = uuid.uuid4().hex
        elif owner.get("token") != mine:
            return None
        data["owner"] = _owner(mine, os.getpid(), start_seq=seq)
        _save(job_dir(root) / QUEUE, data)
        return mine, seq

    return _locked(root, body)


def _finish(root: Path, token: str, start_seq: int, *, owed: bool, progress: bool) -> str | int:
    """The final pending read, then one of: hand the queue to a fresh run (`relaunched`), keep it
    and run another pass now (the new start seq), keep it and wait (`waiting`), or release it
    (`released`). `done_seq` moves only when nothing is owed."""

    def body() -> str | int:
        data = queue(root)
        latest = _fold(root, data)
        pause("final-read")
        if as_dict(data.get("owner")).get("token") != token:
            return "replaced"
        if not owed:
            data["done_seq"] = start_seq
        if latest > start_seq or (owed and progress):
            if _own(root, data, Budget()):
                return "relaunched"
            data["owner"] = _owner(token, os.getpid(), start_seq=latest)
            _save(job_dir(root) / QUEUE, data)
            return latest
        if owed:
            data["owner"] = _owner(token, os.getpid(), start_seq=start_seq)
            _save(job_dir(root) / QUEUE, data)
            return "waiting"
        data["owner"] = None
        _save(job_dir(root) / QUEUE, data)
        return "released"

    return _locked(root, body)


def _wait(root: Path, start_seq: int, seconds: float) -> None:
    """Sleep up to `seconds`, waking early when a newer request is queued or the root is gone."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and root.is_dir() and as_int(queue(root).get("seq")) <= start_seq:
        time.sleep(min(POLL_S, max(0.0, end - time.monotonic())))


def _resume(root: Path, token: str) -> int | None:
    def body() -> int | None:
        data = queue(root)
        seq = _fold(root, data)
        if as_dict(data.get("owner")).get("token") != token:
            return None
        data["owner"] = _owner(token, os.getpid(), start_seq=seq)
        _save(job_dir(root) / QUEUE, data)
        return seq

    return _locked(root, body)


def _expire(root: Path, token: str) -> str:
    """Release the queue at the end of a job's life, leaving its requests for the next trigger."""

    def body() -> str:
        data = queue(root)
        if as_dict(data.get("owner")).get("token") != token:
            return "replaced"
        data["owner"] = None
        _save(job_dir(root) / QUEUE, data)
        return "expired"

    return _locked(root, body)


class Heartbeat:
    """Renews this run's lease every `HEARTBEAT_S` until stopped, except while one pass has run
    longer than `PASS_MAX_S`: a run stuck in a pass lets its lease end."""

    def __init__(self, root: Path, token: str) -> None:
        self.root, self.token = root, token
        self.pass_began: float | None = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._beat, daemon=True)
        self.thread.start()

    def _beat(self) -> None:
        while not self.stopped.wait(HEARTBEAT_S):
            began = self.pass_began
            if began is not None and time.monotonic() - began > PASS_MAX_S:
                continue
            try:
                with coordination(self.root, HEARTBEAT_S / 2):
                    data = queue(self.root)
                    owner = as_dict(data.get("owner"))
                    if owner.get("token") == self.token:
                        owner["until"] = time.time() + LEASE_S
                        data["owner"] = owner
                        _save(job_dir(self.root) / QUEUE, data)
            except TimeoutError, OSError:
                continue

    def stop(self) -> None:
        self.stopped.set()


def run(root: Path, token: str, do: Callable[[Path], dict[str, Any]] | None = None) -> str:
    """Claim the queue, then publish until nothing is owed, a newer run takes over, or the queue
    is released. `token` is `-` for a claimant."""
    started = _start(root, token)
    if started is None:
        return "not-owner"
    token, start_seq = started
    beat = Heartbeat(root, token)
    try:
        return _passes(root, token, start_seq, do, beat)
    finally:
        beat.stop()


def _passes(
    root: Path, token: str, start_seq: int, do: Callable[[Path], dict[str, Any]] | None, beat: Heartbeat
) -> str:
    delay = RETRY_FIRST_S
    began = time.monotonic()
    while True:
        beat.pass_began = time.monotonic()
        try:
            outcome = (do or work)(root)
        except Exception as exc:  # noqa: BLE001 — a failure is kept and retried, never raised
            outcome = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        beat.pass_began = None
        if not root.is_dir():
            return "root-gone"
        ok = outcome.get("status") in OK
        note(root, last={"at": core.iso(core.now()), "start_seq": start_seq, **outcome})
        if not ok:
            note(
                root,
                failure={"stage": "publish", "detail": f"{outcome.get('status')}: {outcome.get('detail')}"},
            )
        owed = not ok or bool(outcome.get("pending")) or bool(outcome.get("left"))
        progress = ok and bool(outcome.get("progress"))
        decision = _finish(root, token, start_seq, owed=owed, progress=progress)
        if isinstance(decision, int):
            start_seq, delay = decision, RETRY_FIRST_S
            continue
        if decision != "waiting":
            return decision
        if time.monotonic() - began > JOB_MAX_S:
            return _expire(root, token)
        _wait(root, start_seq, delay)
        if not root.is_dir():
            return "root-gone"
        delay = min(delay * 2, RETRY_MAX_S)
        resumed = _resume(root, token)
        if resumed is None:
            return "replaced"
        start_seq = resumed


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else CLAIMANT)
