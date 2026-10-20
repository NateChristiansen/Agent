"""Associate Codex threads with ribbon launch markers through process evidence. Hooks in the managed
daemon inherit its environment, not the TUI's AGENT_RIBBON_LAUNCH marker.

Require a TUI-origin rollout and one interactive TUI starting within the thread UUIDv7 creation
window, with a codex marker in its ancestry. Ambiguity claims nothing. SETTLE_S permits a delayed
TUI thread reference; FRESH_S bounds the sole-TUI fallback for clients creating a thread only at
the first prompt.

Known association limits remain: /clear in an older TUI can race a ribbon TUI whose thread
reference arrives late. A fresh thread whose own TUI just exited can claim the sole surviving older
TUI's marker. These can mislabel a tab; timing is not proof of a unique thread-to-TUI relationship.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .records.narrow import dig

WINDOW_BEFORE_S = 30.0
WINDOW_AFTER_S = 2.0
SETTLE_S = 5.0
FRESH_S = 30.0  # the sole-TUI rule links only a thread created this recently
MAX_ANCESTORS = 4
MARKER_RE = re.compile(r"AGENT_RIBBON_LAUNCH=['\"]?(codex:[0-9A-Fa-f-]{36})")
_LONG_RUNNING = {"app-server"}  # The daemon is never a tab's TUI; any other codex counts as a rival.


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    started: float
    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return " ".join(self.argv)


def thread_created(session_id: str) -> float | None:
    """Epoch seconds from a UUIDv7's first 48 bits, or None for any other id."""
    hexed = session_id.replace("-", "")
    if len(hexed) != 32 or hexed[12] != "7":
        return None
    try:
        return int(hexed[:12], 16) / 1000
    except ValueError:
        return None


def _codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def thread_originator(session_id: str, created: float) -> str:
    """`originator` from the rollout's first line; "" when the rollout cannot be read."""
    sessions = _codex_home() / "sessions"
    for offset in (0, -86400, 86400):
        day = time.strftime("%Y/%m/%d", time.localtime(created + offset))
        for path in (sessions / day).glob(f"rollout-*-{session_id}.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    first = json.loads(fh.readline())
            except OSError, ValueError:
                return ""
            return str(dig(first, "payload", "originator") or "")
    return ""


def _posix_processes() -> list[Process]:
    try:
        out = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,ppid=,lstart=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except OSError, subprocess.SubprocessError:
        return []
    procs: list[Process] = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            started = time.mktime(time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        procs.append(Process(int(parts[0]), int(parts[1]), started, tuple(parts[7].split())))
    return procs


def _windows_processes() -> list[Process]:
    psutil = importlib.import_module("psutil")  # a win32-only dependency, as in win_snapshot
    procs: list[Process] = []
    for p in psutil.process_iter(["pid", "ppid", "create_time", "cmdline"], ad_value=None):
        info = p.info
        argv = tuple(info.get("cmdline") or ())
        if not argv or info.get("create_time") is None:
            continue
        procs.append(Process(int(info["pid"]), int(info.get("ppid") or 0), float(info["create_time"]), argv))
    return procs


def processes() -> list[Process]:
    return _windows_processes() if sys.platform == "win32" else _posix_processes()


def _is_codex_tui(p: Process) -> bool:
    """The native Codex binary, not a node shim, and not the daemon."""
    exe = re.split(r"[\\/]", p.argv[0])[-1].lower() if p.argv else ""
    if exe not in ("codex", "codex.exe"):
        return False
    positional = next((a for a in p.argv[1:] if not a.startswith("-")), "")
    return positional not in _LONG_RUNNING


def _marker_above(p: Process, by_pid: dict[int, Process]) -> str:
    current = p
    for _ in range(MAX_ANCESTORS):
        parent = by_pid.get(current.ppid)
        if parent is None or parent.pid == current.pid:
            return ""
        found = MARKER_RE.search(parent.command)
        if found:
            return found.group(1)
        current = parent
    return ""


def tui_threads() -> list[str] | None:
    """Every thread a Codex TUI created, from the reference file Codex writes at creation.

    A rollout can be written lazily, so it cannot show that a thread exists. This directory had an entry for
    every TUI thread that day, lazy ones included. None when it cannot be read."""
    try:
        return [p.name for p in (_codex_home() / "tui-thread-reference-capabilities").iterdir()]
    except OSError:
        return None


def marker_for(
    session_id: str,
    procs: list[Process] | None = None,
    threads: list[str] | None = None,
    now: float | None = None,
) -> str:
    """The `codex:<uuid>` marker of the ribbon tab that started this thread's TUI, or ""."""
    created = thread_created(session_id)
    if created is None or thread_originator(session_id, created) != "codex-tui":
        return ""
    procs = processes() if procs is None else procs
    by_pid = {p.pid: p for p in procs}
    tuis = [p for p in procs if _is_codex_tui(p)]
    # The TUI must predate the thread: an older thread whose TUI has exited must not take a
    # newer ribbon tab that happens to be the only TUI left.
    # And the thread must be new: a thread whose own TUI exited must not later take an older,
    # unrelated TUI that is now the only one left. A lazy thread's first hooks fire as it is
    # created, so FRESH_S still covers Codex 0.159.2.
    fresh = (time.time() if now is None else now) - created <= FRESH_S
    if len(tuis) == 1 and fresh and tuis[0].started <= created + WINDOW_AFTER_S:
        # One Codex TUI on the machine: a TUI-created thread is its. Codex 0.159.2 creates the
        # thread at the first prompt, which can be long after the TUI starts, beyond the timing
        # window below (fresh Windows clone; safe only when sole).
        return _marker_above(tuis[0], by_pid)
    near = [
        p
        for p in procs
        if _is_codex_tui(p) and created - WINDOW_BEFORE_S <= p.started <= created + WINDOW_AFTER_S
    ]
    if len(near) != 1:
        return ""
    tui = near[0]
    # A TUI creates its own thread about 2 s after it starts. A thread made elsewhere at that
    # moment (a /clear in an older TUI) also lands in this window, and the two cannot be told
    # apart, so either one refuses. Wait until the TUI's own thread
    # must exist, so a thread checked first cannot see an empty window.
    if (time.time() if now is None else now) < tui.started + SETTLE_S:
        return ""
    threads = tui_threads() if threads is None else threads
    if threads is None:
        return ""
    owned = [
        t
        for t in threads
        if (at := thread_created(t)) is not None
        and tui.started - WINDOW_AFTER_S <= at <= tui.started + WINDOW_BEFORE_S
    ]
    if owned != [session_id]:
        return ""
    return _marker_above(tui, by_pid)
