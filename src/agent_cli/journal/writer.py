"""Which machine writes the daily note, and one writer at a time per note."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import cast

from .. import win_hold
from ..core import AgentError, agent_path
from ..records.narrow import dig

KEY = "journal"
MACHINE_KEY = "machine"


UNSET = object()


def configured(root: Path) -> object:
    """The raw `journal.machine` value, or `UNSET` when agent.yaml has no such key. A present
    value that is not a mapping's `machine` is returned as it is, for `refusal` to reject."""
    import yaml

    from ..config_file import registry_path

    try:
        data = yaml.safe_load(registry_path(root).read_text(encoding="utf-8"))
    except OSError, ValueError, yaml.YAMLError:
        return UNSET
    block = dig(data, KEY)
    if block is None:
        return UNSET
    if not isinstance(block, dict):
        return block
    return cast("dict[str, object]", block).get(MACHINE_KEY, UNSET)


def refusal(root: Path) -> str | None:
    from ..dispatch import machine as machine_mod
    from ..dispatch.ids import TAG_RE

    wanted = configured(root)
    if wanted is UNSET:
        return None
    if not isinstance(wanted, str) or not TAG_RE.match(wanted):
        return (
            f"`journal.machine` in agent.yaml is {wanted!r}, not a machine label "
            "(1-8 lowercase letters and digits, starting with a letter); fix it there"
        )
    mine = machine_mod.resolve(root)
    if mine.tag == wanted:
        return None
    here = (
        f"this machine is {mine.tag!r}" if mine.tag else f"this machine has no valid label ({mine.problem})"
    )
    return (
        f"journal notes are written on {wanted!r} (`journal.machine` in agent.yaml), and {here}; "
        f"run the journal there, or change `journal.machine`"
    )


def is_writer(root: Path) -> bool:
    return refusal(root) is None


def require_writer(root: Path) -> None:
    problem = refusal(root)
    if problem is not None:
        raise AgentError(problem)


@contextmanager
def note_lock(root: Path, day: date) -> Generator[None]:
    """One writer at a time per note. The lock file sits under `.agent/`, not beside the note, so
    it never syncs. A write reads the note again after taking it."""
    folder = agent_path(root, "locks")
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        folder / f"journal-{day.isoformat()}.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600
    )
    try:
        with win_hold.blocking(fd):
            yield
    finally:
        os.close(fd)
