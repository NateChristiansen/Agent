"""A pytest plugin for scripts/suite_bench.py: counts the files and directories each test process
creates while it runs, through Python's audit hooks, and writes one count per process to
`$BENCH_COUNT_DIR/<pid>`.

It counts each in-process attempt to create a path that does not exist yet (`open` for writing,
`os.open` with O_CREAT, `mkdir`), deleted later or not, so transient files that retention never
keeps are counted and rewriting an existing file is not . A creation
that then fails is still counted, and files child processes create (git, mostly) are not seen:
the figure compares runs, it is not an exact census.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_created = [0]


def _new(path: object) -> bool:
    """The path does not exist yet, so the call is about to create it (an open of an existing
    file, or of a file descriptor, creates nothing)."""
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        return not os.path.lexists(path)
    except OSError, ValueError:
        return False


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "open" and len(args) >= 3:
        path, mode, flags = args[0], args[1], args[2]
        writes = (isinstance(mode, str) and any(c in mode for c in "wxa")) or (
            mode is None and isinstance(flags, int) and bool(flags & os.O_CREAT)
        )
        if writes and _new(path):
            _created[0] += 1
    elif event == "os.mkdir" and args and _new(args[0]):
        _created[0] += 1


def pytest_configure(config: Any) -> None:
    if os.environ.get("BENCH_COUNT_DIR"):
        sys.addaudithook(_hook)


def pytest_unconfigure(config: Any) -> None:
    where = os.environ.get("BENCH_COUNT_DIR")
    if where:
        Path(where, str(os.getpid())).write_text(str(_created[0]), encoding="utf-8")
