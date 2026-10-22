"""Whether implementation may proceed on a feature: it must be `active`, owe no reconciliation
items, and a launch must not name a captured task still awaiting approval. Each check re-resolves
the feature from disk, so a status change after a caller loaded it still counts.

Launch, resume, the supervisor and landing call these and fail closed; edit hooks gate by status
only (`edit_refusal`). Reads and every read-only launch pass without a check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import features as features_mod
from . import reconcile
from .core import AgentError
from .records.narrow import as_dict

OWED_SHOWN = 5


def is_implementation(kind: str | None, writes: list[str] | None) -> bool:
    """Every dispatch is implementation unless it is a probe or declares `writes=[]`."""
    return not (kind == "probe" or writes == [])


def recovery_text(feature: int | str | None = None) -> str:
    n = str(feature) if feature is not None else "<n>"
    return (
        f"map this session with `feature_use {n}`, or bind this worktree with the `harness` tool "
        f"action `worktree_new` (name, feature={n})"
    )


def _resolve(root: Path, feature: features_mod.Feature | int | str) -> features_mod.Feature:
    ref = feature.number if isinstance(feature, features_mod.Feature) else str(feature)
    return features_mod.resolve(root, ref)


def _inactive(f: features_mod.Feature) -> str | None:
    if f.status == "active":
        return None
    return (
        f"{f.label} is {f.status or 'unset'}; implementation needs it active "
        "(`feature_set` status=active with a reason, once the owner says to proceed)"
    )


def _owed(root: Path, f: features_mod.Feature) -> tuple[str | None, reconcile.Ledger]:
    led = reconcile.ledger(root, f)
    owed = led.owed
    if not owed:
        return None, led
    shown = "; ".join(
        f"{i.source} {i.item}" + (f" ({i.reason})" if i.reason else "") for i in owed[:OWED_SHOWN]
    )
    more = f" and {len(owed) - OWED_SHOWN} more" if len(owed) > OWED_SHOWN else ""
    return (
        f"{f.label} has {len(owed)} item(s) to reconcile: {shown}{more}; settle them with the `reconcile` tool",
        led,
    )


def feature_refusal(root: Path, feature: features_mod.Feature | int | str) -> str | None:
    """Why implementation on `feature` is refused now (inactive, or items owed), or None."""
    f = _resolve(root, feature)
    inactive = _inactive(f)
    if inactive:
        return inactive
    owed, _led = _owed(root, f)
    return owed


def launch_refusal(
    root: Path,
    feature: features_mod.Feature | int | str,
    *,
    kind: str | None,
    writes: list[str] | None,
    tasks: list[int] | None = None,
) -> str | None:
    """Why this launch or resume is refused, or None. Read-only launches are never refused."""
    if not is_implementation(kind, writes):
        return None
    f = _resolve(root, feature)
    inactive = _inactive(f)
    if inactive:
        return inactive
    owed, led = _owed(root, f)
    if owed:
        return owed
    held = sorted(n for n in tasks or [] if n in led.holds)
    if held:
        named = ", ".join(f"#{n} (cap {led.holds[n]})" for n in held)
        return f"captured task {named} awaits approval: `reconcile` approve with the capture and evidence"
    return None


def require_launch(
    root: Path,
    feature: features_mod.Feature | int | str,
    *,
    kind: str | None,
    writes: list[str] | None,
    tasks: list[int] | None = None,
) -> None:
    why = launch_refusal(root, feature, kind=kind, writes=writes, tasks=tasks)
    if why:
        raise AgentError(why)


def member_feature(root: Path, member: dict[str, Any]) -> int | None:
    """The feature a landing member belongs to: its dispatch record's, else its branch's manager
    worktree binding, else None (unbound)."""
    dispatch_id = member.get("dispatch_id")
    if dispatch_id:
        from .dispatch import state

        feature = as_dict(state.load(root, str(dispatch_id))).get("feature")
        if isinstance(feature, int):
            return feature
    branch = member.get("branch")
    return bound_feature(root, str(branch)) if branch else None


def bound_feature(root: Path, branch: str) -> int | None:
    from . import worktree

    feature = (worktree.binding(root, branch) or {}).get("feature")
    return feature if isinstance(feature, int) else None


def land_refusals(root: Path, members: list[dict[str, Any]]) -> list[str]:
    """One reason per member that may not land; a non-empty list refuses the whole batch. Each
    member is `{branch, dispatch_id?}`."""
    out: list[str] = []
    for member in members:
        branch = str(member.get("branch") or "?")
        try:
            feature = member_feature(root, member)
        except AgentError as exc:
            out.append(f"{branch}: {exc.message}")
            continue
        if feature is None:
            out.append(f"{branch}: no dispatch record or owning feature; {recovery_text()}")
            continue
        try:
            why = feature_refusal(root, feature)
        except AgentError as exc:
            why = exc.message
        if why:
            out.append(f"{branch}: {why}")
    return out


def session_feature(root: Path, session_id: str | None) -> int | None:
    if not session_id:
        return None
    from . import sessions

    record = sessions.get(root, session_id) or {}
    feature = as_dict(record.get("feature")).get("id")
    return feature if isinstance(feature, int) else None


def edit_refusal(root: Path, *, session_id: str | None, branch: str | None = None) -> str | None:
    """Why a manager edit of guarded code is refused: no owning feature, or one not active.
    `branch` is the manager worktree's branch when the edit lands in one; its binding decides,
    else the session's mapped feature."""
    owner = bound_feature(root, branch) if branch else None
    if owner is None:
        owner = session_feature(root, session_id)
    if owner is None:
        return f"no feature owns this edit; {recovery_text()}"
    try:
        f = _resolve(root, owner)
    except AgentError as exc:
        return f"{exc.message}; {recovery_text(owner)}"
    inactive = _inactive(f)
    return f"{inactive}; {recovery_text(f.id)}" if inactive else None
