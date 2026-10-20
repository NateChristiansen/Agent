"""Windows process inventory with inherited ownership tags and creation identities."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from .proc_tags import TAG_DISPATCH, TAG_ROOT, TAG_SESSION
from .procs import Proc
from .records.narrow import as_dict_or_none


def dispatch_leader(root: Path, dispatch_id: str) -> int:
    """A missing dispatch record must not prevent cleanup of known tagged processes."""
    try:
        from .dispatch import load

        return int(load(root, dispatch_id).get("claude_pid") or 0)
    except Exception:  # noqa: BLE001 — backstop reaping must never fail a dispatch
        return 0


def _enrich(psutil: ModuleType, pid: int, exe: str) -> tuple[float | None, str, dict[str, str], str]:
    """A failed per-process open must not remove the Toolhelp inventory row."""
    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        return None, exe, {}, type(exc).__name__
    try:
        command = " ".join(process.cmdline()) or exe
    except psutil.NoSuchProcess, psutil.AccessDenied, OSError:
        command = exe
    created: float | None
    try:
        created = process.create_time()
    except psutil.NoSuchProcess, psutil.AccessDenied, OSError:
        created = None
    try:
        env = as_dict_or_none(process.environ())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        return created, command, {}, type(exc).__name__
    if env is None:
        return created, command, {}, "InvalidEnvironment"
    return created, command, {k: v for k, v in env.items() if isinstance(v, str)}, ""


def _toolhelp() -> tuple[Callable[[], list[tuple[int, int, str]]], Callable[[int], str]]:
    from .winproc import (
        pid_started,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType] — win32-only
        process_entries,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType] — win32-only
    )

    return process_entries, pid_started  # pyright: ignore[reportUnknownVariableType] — win32-only


def snapshot() -> list[Proc]:
    """Unknown metadata has no reaping identity; an inaccessible parent stays attached."""
    psutil = importlib.import_module("psutil")
    process_entries, pid_started = _toolhelp()

    entries = process_entries()
    started = {pid: pid_started(pid) for pid, _, _ in entries}
    details = {pid: _enrich(psutil, pid, exe) for pid, _, exe in entries}
    retry = [(pid, exe) for pid, _, exe in entries if details[pid][3] in ("NoSuchProcess", "OSError")]
    if retry:
        time.sleep(0.05)
        for pid, exe in retry:
            details[pid] = _enrich(psutil, pid, exe)
    birth = {pid: details[pid][0] for pid, _, _ in entries}
    out: list[Proc] = []
    for pid, ppid, _exe in entries:
        created, command, env, tag_error = details[pid]
        parent_created = birth.get(ppid)
        escaped = ppid not in birth or bool(created and parent_created and parent_created > created)
        out.append(
            Proc(
                pid=pid,
                ppid=ppid,
                pgid=pid,
                elapsed=str(max(0, int(time.time() - created))) if created else "",
                command=command,
                session=env.get(TAG_SESSION, ""),
                dispatch=env.get(TAG_DISPATCH, ""),
                root=env.get(TAG_ROOT, ""),
                started=started[pid],
                escaped_hint=escaped,
                tag_error=tag_error,
                created=created,
            )
        )
    return out
