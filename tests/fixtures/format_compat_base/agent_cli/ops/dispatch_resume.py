# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Literal
from .. import dispatch as dispatch_mod
from ._shared import Op
from .dispatch import dispatch_line, shown_record


def dispatch_resume(
    root: Path,
    *,
    dispatch_id: str,
    answer: str,
    answer_by: Literal["manager", "owner"] = "owner",
    budget_usd: float | None = None,
    budget_reason: str | None = None,
    timeout_minutes: float | None = None,
    watch: bool = False,
    allow_unmatched: bool = False,
    again: bool = False,
    writes: list[str] | None = None,
) -> Op:
    d = dispatch_mod.resume(
        root,
        dispatch_id=dispatch_id,
        answer=answer,
        answer_by=answer_by,
        budget=budget_usd,
        budget_reason=budget_reason,
        timeout_minutes=timeout_minutes,
        watch=watch,
        again=again,
        allow_unmatched=allow_unmatched,
        writes=writes,
    )
    d = shown_record(d)
    return Op(d, dispatch_line(d))
