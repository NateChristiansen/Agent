# ruff: noqa
from __future__ import annotations
import subprocess


class CleanupUnconfirmed(subprocess.TimeoutExpired):
    def __str__(self) -> str:
        return super().__str__() + "; process-tree cleanup could not be confirmed"


def text_of(data: str | bytes | None) -> str | None:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data
