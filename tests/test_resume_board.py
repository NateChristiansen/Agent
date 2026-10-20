"""A resume and the path board : the resume takes the board lock
before the allocation lock, as a new launch does, and publishes a reservation for its paths that
the board reads until its queued record is saved, so no launch sees its slot without its paths."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, lifecycle, write_scope
from dispatch_helpers import (
    _new_fast as new_fast,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)
from dispatch_helpers import (
    droot,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture, used as a parameter name
    envelope,
    one,
)

# A skip, not an import error, so collection succeeds on Windows.
fcntl = pytest.importorskip("fcntl", reason="POSIX-only: the probe reads the board lock with flock")


def _board_locked(root: Path) -> bool:
    path = write_scope.board_dir(root) / "board.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def test_a_resume_holds_the_board_before_allocation_and_until_saved(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
):
    fake_claude.scenario([one(envelope())])
    d: dict[str, Any] = new_fast(droot)
    real_guard = allocation.alloc_guard
    seen: list[str] = []

    @contextmanager
    def guard(root: Path, short: str) -> Generator[None]:
        seen.append("board held at alloc" if _board_locked(root) else "board free at alloc")
        with real_guard(root, short):
            yield

    real_save = lifecycle.save

    def save(root: Path, record: dict[str, Any]) -> None:
        if record.get("state") == "queued" and record["id"] == d["id"]:
            on_board = d["id"] in [name for name, _, _ in write_scope.board(root)]
            seen.append("reserved at save" if on_board else "not on the board at save")
            raise AgentError("stop here; the order is what this test checks")
        real_save(root, record)

    monkeypatch.setattr(allocation, "alloc_guard", guard)
    monkeypatch.setattr(lifecycle, "save", save)
    with pytest.raises(AgentError, match="stop here"):
        lifecycle._prepare_resume(droot, dispatch_id=d["id"], answer="go on")  # pyright: ignore[reportPrivateUsage]
    assert seen == ["board held at alloc", "reserved at save"]
    assert d["id"] not in [name for name, _, _ in write_scope.board(droot)], (
        "a failed resume drops its reservation"
    )
