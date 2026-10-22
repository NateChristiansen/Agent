"""Expose the reconciliation ledger as one typed manager tool."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from ..ops import reconcile as reconcile_ops
from .tools import FEATURE_REF, SESSION_ARG, Ctx, Op, ToolSpec


class Decision(TypedDict):
    source: str
    item: str
    disposition: Literal["task", "new-task", "no-task", "waiting", "not-accepted"]
    task: NotRequired[int]
    text: NotRequired[str]
    reason: NotRequired[str]
    on: NotRequired[str]
    until: NotRequired[str]


class Approval(TypedDict):
    of: str
    evidence: str


def reconcile(
    ctx: Ctx,
    feature: str = "current",
    items: list[Decision] | None = None,
    add: list[str] | None = None,
    approve: list[Approval] | None = None,
    session_id: str | None = None,
) -> Op:
    return reconcile_ops.reconcile(
        ctx.root,
        feature=feature,
        session_id=ctx.session_for(feature, session_id),
        items=[dict(i) for i in items] if items else None,
        add=add,
        approve=[dict(a) for a in approve] if approve else None,
    )


RECONCILE_TOOLS: dict[str, ToolSpec] = {
    "reconcile": ToolSpec(
        reconcile,
        "Reconcile results",
        "The feature's ledger of worker results, findings, acceptances and subagent returns that need a "
        "decision. With no `items`, `add` or `approve` it only lists owed, waiting, conflicting and malformed "
        "items and held tasks. `items` decides: disposition `task` (task: an existing task covers it), "
        "`new-task` (text: captures a new task, held from launch until approved), `no-task` (reason: declined, "
        "optional, duplicate or none), `waiting` (on: owner, external, manager, feature:NNN or dispatch:<id>; "
        "until), `not-accepted` (reason; task#n items only, the task stays open). `add` records new items; "
        "`approve` ({of: capture, evidence}) lifts a capture's hold. Reading a result settles nothing. "
        + FEATURE_REF
        + " "
        + SESSION_ARG,
    ),
}
