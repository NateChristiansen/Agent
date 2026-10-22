"""`dispatch` action `plan-ledger`: read a plan's findings ledger, or record the manager's
disposition of one finding or its link to an earlier one (`plan_review_ledger.py` holds the
ledger). `code-ledger` does the same for a branch through `ledger_op`."""

from __future__ import annotations

from pathlib import Path

from .. import plan_review_ledger as ledger_mod
from ..core import AgentError
from ._shared import Op


def dispatch_plan_ledger(
    root: Path,
    *,
    paths: list[str],
    finding_id: str | None = None,
    disposition: str | None = None,
    note: str | None = None,
    reraise_of: str | None = None,
) -> Op:
    """With only `paths`, the ledger and its disputes. With `finding_id`, record `disposition`
    (and `note`: the changed section or the reason) or link it as a re-raise of `reraise_of`."""
    if len(paths) != 1:
        raise AgentError("plan-ledger takes exactly one plan path in `paths`")
    return ledger_op(
        root,
        paths[0],
        action="plan-ledger",
        finding_id=finding_id,
        disposition=disposition,
        note=note,
        reraise_of=reraise_of,
    )


def ledger_op(
    root: Path,
    subject: str | ledger_mod.Book,
    *,
    action: str,
    finding_id: str | None,
    disposition: str | None,
    note: str | None,
    reraise_of: str | None,
) -> Op:
    """Read `subject`'s ledger, or record one disposition or re-raise link (`dispatch_plan_ledger`)."""
    if finding_id is None:
        if disposition is not None or reraise_of is not None:
            raise AgentError(f"{action} needs `finding_id` to record a disposition or a re-raise")
    elif disposition is not None and reraise_of is not None:
        raise AgentError(f"{action} records a disposition or a re-raise link, not both at once")
    elif disposition is not None:
        entry = ledger_mod.dispose(
            root, subject, finding_id=finding_id, disposition=disposition, note=note or ""
        )
        return Op(entry, f"{finding_id}: {disposition}.")
    elif reraise_of is not None:
        entry = ledger_mod.link_reraise(root, subject, finding_id=finding_id, earlier_id=reraise_of)
        return Op(entry, f"{finding_id} re-raises {reraise_of}.")
    else:
        raise AgentError(f"{action} with `finding_id` needs `disposition` or `reraise_of`")
    data = ledger_mod.load(root, subject)
    found = ledger_mod.disputes(data)
    entries = ledger_mod.findings(data)
    open_ids = [str(e["id"]) for e in entries if e.get("disposition") is None]
    return Op(
        {**data, "disputes": found},
        f"{len(entries)} finding(s), {len(open_ids)} without a disposition, {len(found)} dispute(s) "
        "for the owner.",
    )
