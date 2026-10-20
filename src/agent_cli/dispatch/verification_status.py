"""Read verification state while another process writes it. Tolerate concurrent store changes and
preserve unknown liveness rather than infer safe cleanup.
"""

from __future__ import annotations

import calendar
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import AgentError
from . import verification_process as procs
from .verification_evidence import Binding, Evidence
from .verification_store import Store

# Enough to outlast one command handover, which is the only window that moves the lifecycle
# out from under a status read.
STATUS_READ_ATTEMPTS = 4
# `ps -o lstart` has whole seconds; this also absorbs a small clock step.
START_SLACK = 1.0
_PS_ENV = {**procs.BASE_ENV, "TZ": "UTC0"}


@dataclass(frozen=True)
class RunStatus:
    evidence: Evidence | None
    lifecycle: dict[str, Any]
    active: bool
    cleanup_confirmed: bool

    @property
    def requires_lease(self) -> bool:
        return self.active or not self.cleanup_confirmed


def latest(store: Store, life: dict[str, Any]) -> Evidence | None:
    raw = store.read("terminal.json")
    if raw is None:
        count = (life["command_index"] if life["command_index"] is not None else -1) + 1
        for index in (count, max(0, count - 1)):
            raw = store.read(f"evidence-{index:05d}.json")
            if raw is not None:
                break
    raw = raw if raw is not None else store.read("initial.json")
    evidence = Evidence.from_dict(raw) if raw is not None else None
    if evidence is not None and evidence.binding != store.binding:
        raise ValueError("stale verifier evidence binding")
    return evidence


def cleanup_record(store: Store, life: dict[str, Any]) -> dict[str, Any] | None:
    return store.cleanup_record(life)


def absent(number: int, *, group: bool = False) -> bool:
    """POSIX: True when the kernel says no process, or no process group, has this number. That
    answer needs no `ps`, and `ps` is what a loaded machine slows down: during a landing run one
    `ps -axo` in about 800 took over a second, up to 5.3 s, and a probe that times out reads as
    alive. Status reads whose processes were long gone kept the lease. Any other
    answer, a zombie or another user's process included, is left to the `ps` reading."""
    if number <= 0:
        return False
    try:
        (os.killpg if group else os.kill)(number, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _lstart(fields: Sequence[str]) -> float | None:
    try:
        return calendar.timegm(time.strptime(" ".join(fields), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _later(fields: Sequence[str], since: float) -> bool:
    started = _lstart(fields)
    return started is not None and started > since + START_SLACK


def process_gone(pid: int | None, since: float | None) -> bool:
    """`process_dead`, except that a live process started after `since`, a store write that
    already named `pid`, holds a reused number and is not the run's. With no `since`, or a start
    time that cannot be read, the number alone decides, as it always did."""
    if pid is None or since is None:
        return procs.process_dead(pid)
    if os.name == "nt":
        from . import verification_win as win

        return win.process_gone(pid, since + START_SLACK)
    if absent(pid):
        return True
    try:
        argv = ["/bin/ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)]
        code, output = procs.capture(argv, Path("/"), _PS_ENV, 1, 1024)
    except OSError, AgentError, subprocess.TimeoutExpired:
        return False
    fields = output.decode("ascii", "replace").split()
    if code == 1 and not fields:
        return True
    if code != 0 or not fields:
        return False
    return fields[0].startswith("Z") or _later(fields[1:], since)


def group_gone(pgid: int, since: float | None) -> bool:
    """`group_dead`, ignoring members that started after `since`. Only pass `since` when no
    member of the run's own group can start after it. On Windows the group is the job named for
    `pgid` and the process under that number; only the process carries a start time, so the job
    is still judged by its name alone."""
    if since is None:
        return procs.group_dead(pgid)
    if os.name == "nt":
        from . import verification_win as win

        try:
            return win.job_empty(pgid) and win.process_gone(pgid, since + START_SLACK)
        except OSError:
            return False
    if absent(pgid, group=True):
        return True
    try:
        argv = ["/bin/ps", "-axo", "pgid=", "-o", "stat=", "-o", "lstart="]
        code, output = procs.capture(argv, Path("/"), _PS_ENV, 1, 4 * 1024 * 1024)
        if code:
            return False
        for row in output.decode("ascii").splitlines():
            fields = row.split()
            if len(fields) != 7 or not fields[0].isdigit():
                return False
            if int(fields[0]) == pgid and not fields[1].startswith("Z") and not _later(fields[2:], since):
                return False
        return True
    except OSError, ValueError, AgentError, subprocess.TimeoutExpired:
        return False


def _status(store: Store, life: dict[str, Any]) -> RunStatus:
    """Pids are checked against the last write that names them: every recorded process started
    before the lifecycle write that holds its pid, and the watchdog before the cleanup record
    it writes and then exits. A confirmed cleanup record means the command group was empty, and
    a finished guardian starts nothing more, so later members of those groups are reuse too."""
    evidence = latest(store, life)
    written = store.modified("lifecycle.json")
    active = not process_gone(life["guardian_pid"], written)
    if life["guardian_pid"] is None and life["phase"] == "created":
        active = not process_gone(life["creator_pid"], written)
    guardian_clear = life["guardian_pid"] is None or group_gone(
        life["guardian_pid"], written if life["finished"] else None
    )
    cleaned = (
        not active and guardian_clear and (life["cleanup_confirmed"] or life["guardian_pid"] is not None)
    )
    if life["command_pid"] is not None:
        cleanup = cleanup_record(store, life)
        confirmed = bool(cleanup and cleanup["cleanup_confirmed"])
        cleaned_at = store.modified(f"cleanup-{life['command_index']:05d}.json") if cleanup else None
        cleaned = (
            not active
            and guardian_clear
            and group_gone(life["command_pid"], cleaned_at if confirmed else None)
        )
        if life["watchdog_pid"] is not None:
            cleaned = (
                cleaned
                and confirmed
                and process_gone(life["watchdog_pid"], cleaned_at if cleanup else written)
            )
    return RunStatus(evidence, life, active, cleaned)


def load_status(root: Path, binding: Binding) -> RunStatus:
    store = Store(root, binding)
    try:
        # lifecycle.json and cleanup-N.json are two files, read one after the other, and the
        # runner keeps writing while this reads. A lifecycle snapshot taken before a command's
        # watchdog pid was recorded, paired with the cleanup file that watchdog wrote a moment
        # later, disagree — and that is two instants, not a stale record. So on a mismatch,
        # re-read the lifecycle: if it moved, the snapshot was the problem and we try again with
        # the newer one. If it did not move, the record really is invalid and the error stands.
        for _ in range(STATUS_READ_ATTEMPTS):
            life = store.load_lifecycle()
            try:
                return _status(store, life)
            except ValueError:
                if store.load_lifecycle() == life:
                    raise
        raise AgentError("verifier lifecycle did not settle while reading run status")
    finally:
        store.close()
