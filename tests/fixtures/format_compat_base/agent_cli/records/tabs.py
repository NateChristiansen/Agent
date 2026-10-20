# ruff: noqa
from __future__ import annotations
from collections.abc import Mapping
from typing import TypedDict, cast
from .narrow import as_dict


class PaintedRow(TypedDict, total=False):
    label: str | None
    name: str
    at: float


class AttemptRow(TypedDict, total=False):
    label: str
    match_version: int | None
    count: int


def painted_of(state: Mapping[str, object]) -> dict[str, PaintedRow]:
    return cast("dict[str, PaintedRow]", as_dict(state.get("painted")))


def attempts_of(state: Mapping[str, object]) -> dict[str, AttemptRow]:
    return cast("dict[str, AttemptRow]", as_dict(state.get("attempts")))
