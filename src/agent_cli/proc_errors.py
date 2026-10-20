"""Errors and output handling shared by `procs.run_bounded` and its callers."""

from __future__ import annotations

import subprocess


class CleanupUnconfirmed(subprocess.TimeoutExpired):
    """The command timed out, but its Windows descendant cleanup was not confirmed."""

    def __str__(self) -> str:
        return super().__str__() + "; process-tree cleanup could not be confirmed"


def text_of(data: str | bytes | None) -> str | None:
    """`TimeoutExpired` carries partial output as bytes even from a text-mode pipe."""
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data
