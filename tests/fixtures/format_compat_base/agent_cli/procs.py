# ruff: noqa
from __future__ import annotations
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from . import core
from .core import atomic_write, iso, now
from .proc_errors import CleanupUnconfirmed, text_of
from .proc_tags import TAG_DISPATCH, TAG_ROOT, TAG_SESSION, linux_tags, ps_tags, resolved_root
from .records.narrow import as_dict, as_dict_or_none

KEEP_FILE = core.agent_path(Path(), "keep.json")
REAP_GRACE_SECONDS = 5.0
"How long a group gets between SIGTERM and SIGKILL. Long enough for a package manager to write\nits lockfile and exit cleanly; short enough that a hook or a return is not held up by it."
_SUPERVISOR_RE = re.compile("\\bagent(?:\\.py)?\\s+dispatch\\s+run\\s+(\\S+)")


def run_bounded(
    cmd: str | list[str],
    *,
    timeout: float,
    shell: bool = False,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    windows = sys.platform == "win32"
    proc = subprocess.Popen(
        cmd,
        shell=shell,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=not windows,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if windows else 0,
    )
    job = None
    try:
        try:
            if windows:
                from .win_tree import Job

                try:
                    job = Job(proc)
                except OSError:
                    pass
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if job is not None:
                job.terminate()
            else:
                stopped = kill_group(proc.pid)
                if windows and (not stopped):
                    raise CleanupUnconfirmed(
                        cmd, timeout, output=text_of(exc.output), stderr=text_of(exc.stderr)
                    ) from None
            try:
                out, err = proc.communicate(timeout=REAP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = ("", "")
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=REAP_GRACE_SECONDS)
        raise
    finally:
        if job is not None:
            job.close()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError, PermissionError:
        return False
    return True


def kill_group(pgid: int, grace: float = REAP_GRACE_SECONDS) -> bool:
    if sys.platform == "win32":
        from .win_tree import kill_tree

        return kill_tree(pgid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and _group_alive(pgid):
        time.sleep(0.1)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass
    return True


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int
    elapsed: str
    command: str
    session: str = ""
    dispatch: str = ""
    started: str = ""
    root: str = ""
    escaped_hint: bool | None = None
    tag_error: str = ""
    created: float | None = None

    @property
    def escaped(self) -> bool:
        return self.escaped_hint if self.escaped_hint is not None else self.ppid == 1

    @property
    def supervisor_of(self) -> str:
        m = _SUPERVISOR_RE.search(self.command)
        return m.group(1) if m else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "elapsed": self.elapsed,
            "command": self.command,
            "session": self.session,
            "dispatch": self.dispatch,
            "started": self.started,
            "escaped": self.escaped,
            "supervisor_of": self.supervisor_of,
            "tag_error": self.tag_error,
        }


def _ps(args: list[str]) -> str:
    try:
        return subprocess.run(["ps", *args], capture_output=True, text=True, check=False, timeout=15).stdout
    except OSError, subprocess.SubprocessError:
        return ""


def _read_tags(args: list[str], read: set[int], tags: dict[int, dict[str, str]]) -> None:
    ps_tags(_ps(["-ww", "-E", "-o", "pid=,command=", *args]), read, tags)


def snapshot() -> list[Proc]:
    if sys.platform == "win32":
        from .win_snapshot import snapshot as windows_snapshot

        return windows_snapshot()
    tags: dict[int, dict[str, str]] = {}
    read: set[int] = set()
    linux = sys.platform.startswith("linux")
    if not linux:
        _read_tags(["-A"], read, tags)
    lines = _ps(["-A", "-ww", "-o", "pid=,ppid=,pgid=,etime=,command="]).splitlines()
    rows = [p for line in lines if len((p := line.strip().split(None, 4))) == 5 and p[0].isdigit()]
    if linux:
        read, tags = linux_tags([int(p[0]) for p in rows])
    elif late := [p[0] for p in rows if int(p[0]) not in read]:
        _read_tags(["-p", ",".join(late)], read, tags)

    def row(pid: int, ppid: str, pgid: str, elapsed: str, command: str) -> Proc:
        t = tags.get(pid, {})
        return Proc(
            pid=pid,
            ppid=int(ppid),
            pgid=int(pgid),
            elapsed=elapsed,
            command=command,
            session=t.get(TAG_SESSION, ""),
            dispatch=t.get(TAG_DISPATCH, ""),
            root=t.get(TAG_ROOT, ""),
            tag_error="" if pid in read or not _alive(pid) else "tag read failed",
        )

    return [row(int(p[0]), *p[1:]) for p in rows]


def harness_owned(procs: list[Proc]) -> list[Proc]:
    return [p for p in procs if p.session or p.dispatch]


def process_started(pid: int) -> str:
    if sys.platform == "win32":
        from .winproc import pid_started

        return pid_started(pid)
    return _ps(["-o", "lstart=", "-p", str(pid)]).strip()


def _read_keep(root: Path) -> dict[str, object]:
    try:
        data = json.loads((root / KEEP_FILE).read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    return as_dict(data)


def kept(root: Path) -> dict[int, dict[str, Any]]:
    raw = _read_keep(root)
    live: dict[int, dict[str, Any]] = {}
    for key, value in raw.items():
        if not str(key).isdigit() or (entry := as_dict_or_none(value)) is None:
            continue
        pid = int(key)
        if entry.get("started") and process_started(pid) == entry["started"]:
            live[pid] = entry
    if len(live) != len(raw):
        atomic_write(root / KEEP_FILE, json.dumps({str(k): v for k, v in live.items()}, indent=2) + "\n")
    return live


def keep(root: Path, pid: int, note: str = "") -> dict[str, Any]:
    started = process_started(pid)
    if not started:
        return {"pid": pid, "kept": False, "reason": "no such process"}
    command = "" if sys.platform == "win32" else _ps(["-o", "command=", "-p", str(pid)]).strip()
    entries = {str(k): v for k, v in kept(root).items()}
    entries[str(pid)] = {"started": started, "command": command, "note": note, "kept_at": iso(now())}
    atomic_write(root / KEEP_FILE, json.dumps(entries, indent=2) + "\n")
    return {"pid": pid, "kept": True, "command": command, "note": note}


def unkeep(root: Path, pid: int) -> dict[str, Any]:
    entries = {str(k): v for k, v in kept(root).items()}
    was = entries.pop(str(pid), None)
    atomic_write(root / KEEP_FILE, json.dumps(entries, indent=2) + "\n")
    return {"pid": pid, "unkept": was is not None}


def _protected(root: Path) -> set[int]:
    return {os.getpid(), os.getppid(), *kept(root)}


def select(
    procs: list[Proc],
    *,
    session: str = "",
    dispatch: str = "",
    root: Path | None = None,
    escaped_only: bool = False,
) -> list[Proc]:
    out: list[Proc] = []
    resolved = resolved_root(str(root)) if root is not None else ""
    for p in procs:
        if p.supervisor_of:
            continue
        if escaped_only and (not p.escaped):
            continue
        if session and p.session == session:
            out.append(p)
        elif (
            dispatch
            and p.dispatch == dispatch
            and (root is None or (resolved and resolved_root(p.root) == resolved))
        ):
            out.append(p)
    return out


def select_windows_reap(
    *, dispatch: str, leader_pid: int = 0, root: Path | None = None
) -> tuple[list[Proc], list[Proc]]:
    rows = snapshot()
    targets = select(rows, dispatch=dispatch, root=root)
    related = {leader_pid, *(p.pid for p in targets)} - {0}
    unknown: list[Proc] = []
    pending = [p for p in rows if p.tag_error and p.started and (process_started(p.pid) == p.started)]
    while pending:
        found = [p for p in pending if p.ppid in related]
        if not found:
            break
        unknown.extend(found)
        related.update((p.pid for p in found))
        pending = [p for p in pending if p not in found]
    return (targets, unknown)


def reap(root: Path, targets: list[Proc], *, grace: float = REAP_GRACE_SECONDS) -> dict[str, Any]:
    protected = _protected(root)
    keep_pids = set(kept(root))
    skipped = [p.pid for p in targets if p.pid in keep_pids]
    live = [p for p in targets if p.pid not in protected]
    if sys.platform == "win32":
        psutil = importlib.import_module("psutil")
        terminated: list[int] = []
        for p in live:
            if not p.started:
                continue
            try:
                process = psutil.Process(p.pid)
                process.create_time()
                if process_started(p.pid) != p.started:
                    continue
                process.kill()
                terminated.append(p.pid)
            except psutil.NoSuchProcess, psutil.AccessDenied:
                continue
        return {"terminated": terminated, "killed": [], "skipped_kept": skipped}
    signalled: list[int] = []
    for p in live:
        try:
            os.kill(p.pid, signal.SIGTERM)
            signalled.append(p.pid)
        except ProcessLookupError, PermissionError:
            continue
    deadline = time.monotonic() + grace
    remaining = list(signalled)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _alive(pid)]
        if remaining:
            time.sleep(0.1)
    killed: list[int] = []
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError, PermissionError:
            continue
    return {"terminated": signalled, "killed": killed, "skipped_kept": skipped}


def reap_dispatch(root: Path, dispatch_id: str, *, leader_pid: int = 0) -> dict[str, Any]:
    try:
        if sys.platform == "win32":
            if not leader_pid:
                from .win_snapshot import dispatch_leader

                leader_pid = dispatch_leader(root, dispatch_id)
            targets, unknown = select_windows_reap(dispatch=dispatch_id, leader_pid=leader_pid, root=root)
        else:
            targets, unknown = (select(snapshot(), dispatch=dispatch_id, root=root), list[Proc]())
        report: dict[str, Any] = (
            reap(root, targets) if targets else {"terminated": [], "killed": [], "skipped_kept": []}
        )
        if unknown:
            report["error"] = True
            report["unverified_pids"] = [p.pid for p in unknown]
        return report
    except Exception:
        return {"terminated": [], "killed": [], "skipped_kept": [], "error": True}


LIVE_DISPATCH_STATES = ("queued", "running")
SUPERVISOR_EXIT_SECONDS = 60.0
"How long after its dispatch went terminal a supervisor is still exiting rather than stuck.\n`cancel` writes `cancelled` before the supervisor has interrupted the worker, finalized and run\nits own reap (each bounded by a 5 s grace), so a `procs_list` right after a cancel saw a supervisor\ndoing exactly that and called it stuck."


def _dispatch_record(
    root: Path, dispatch_id: str, cache: dict[str, dict[str, Any] | None]
) -> dict[str, Any] | None:
    if dispatch_id not in cache:
        try:
            from .dispatch import load

            cache[dispatch_id] = load(root, dispatch_id)
        except Exception:
            cache[dispatch_id] = None
    return cache[dispatch_id]


def _dispatch_live(root: Path, dispatch_id: str, cache: dict[str, dict[str, Any] | None]) -> bool:
    d = _dispatch_record(root, dispatch_id, cache)
    return d is not None and str(d.get("state")) in LIVE_DISPATCH_STATES


def _supervisor_exiting(root: Path, dispatch_id: str, cache: dict[str, dict[str, Any] | None]) -> bool:
    d = _dispatch_record(root, dispatch_id, cache)
    if d is None or str(d.get("state")) in LIVE_DISPATCH_STATES:
        return False
    ages: list[float] = []
    for key in ("finished", "cancel_requested"):
        try:
            ages.append((now() - datetime.fromisoformat(str(d.get(key)))).total_seconds())
        except ValueError, TypeError:
            continue
    return bool(ages) and min(ages) < SUPERVISOR_EXIT_SECONDS


def classify(root: Path, procs_: list[Proc]) -> list[dict[str, Any]]:
    attached_sessions = {p.session for p in procs_ if p.session and (not p.escaped)}
    keep_pids = set(kept(root))
    records: dict[str, dict[str, Any] | None] = {}
    rows: list[dict[str, Any]] = []
    for p in harness_owned(procs_):
        if p.dispatch:
            owner, owner_live = (f"dispatch {p.dispatch}", _dispatch_live(root, p.dispatch, records))
        else:
            owner, owner_live = (f"session {p.session[:8]}", p.session in attached_sessions)
        exiting = bool(
            p.supervisor_of and (not owner_live) and _supervisor_exiting(root, p.supervisor_of, records)
        )
        row = p.as_dict()
        row.update(
            owner=owner,
            owner_live=owner_live,
            kept=p.pid in keep_pids,
            orphan=bool(
                p.escaped and (not owner_live) and (p.pid not in keep_pids) and (not p.supervisor_of)
            ),
            supervisor_exiting=exiting,
            stuck_supervisor=bool(p.supervisor_of and (not owner_live) and (not exiting)),
        )
        rows.append(row)
    return rows


def orphans(root: Path, procs_: list[Proc]) -> list[dict[str, Any]]:
    return [r for r in classify(root, procs_) if r["orphan"]]


def _alive(pid: int) -> bool:
    return core.is_alive(pid)
