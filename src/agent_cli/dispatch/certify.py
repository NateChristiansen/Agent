"""A full harness attempt certified once, as the pass record `agent land` reuses.

Moved out of verification_gate.py. Under selection policy 2 (see ADR-0045) a dispatch never runs
the
full suite, so this applies only to dispatches launched under policy 0 or 1.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from .. import verify_record
from ..core import AgentError, iso, now
from . import telemetry_events, test_select
from .state import save
from .verification_evidence import INFRASTRUCTURE, Binding, Evidence, config_digest


def certify(root: Path, dispatch_id: str, attempt: int, d: dict[str, Any], config: Any) -> bool:
    """Run a full harness attempt once, as a pass record `agent land` reuses. False
    when it does not apply: another repo, a dirty tree, a scoped selection."""
    from .lane import give_up, rerun_binding
    from .verification_gate import publish, record_guard, stale
    from .verification_process import observe_tree
    from .verification_store import Store

    if test_select.policy_of(d) == 2:
        return False  # policy 2: the full suite runs only for the landing candidate (see ADR-0045)

    worktree = Path(d["worktree"])
    if not verify_record.covers(root, worktree):
        return False
    try:
        tree = observe_tree(worktree, launch_base=d.get("start_sha"))
    except Exception:  # noqa: BLE001 — anything unknown means the lane run
        return False
    if not (tree.tracked_clean and tree.index_clean):
        return False
    selection = test_select.select_for(worktree, d, tree.head)
    if selection.scope != "full":
        return False
    telemetry_events.transition(root, d, "verifying", via="certificate")
    try:
        found = verify_record.record(root, tree.head, extra=selection.extra)
    except verify_record.RecordFailed as exc:
        with record_guard(root, dispatch_id) as current:
            current["run_area"] = (
                exc.run_dir.relative_to(root).as_posix() if exc.run_dir.is_relative_to(root) else None
            )
            test_select.publish(current, selection, head_sha=tree.head, observed_at=iso(now()))
            save(root, current)
        again = rerun_binding(exc.run_dir, selection.extra)
        infrastructure = exc.category in {"incomplete", "timeout"}
        reason = (INFRASTRUCTURE if infrastructure else "") + f"{exc.category}: {exc}"
        give_up(root, dispatch_id, attempt, "failed", reason, 0.0, again)
        return True
    except AgentError as exc:
        give_up(root, dispatch_id, attempt, "incomplete", f"pass record not made: {exc}", 0.0)
        return True
    event_id = telemetry_events.new_event_id()
    with record_guard(root, dispatch_id) as current:
        if stale(current, attempt) or current.get("verification") is not None:
            return True
        binding = Binding(
            current["id"], current["attempt"], tree.head, config_digest(config), secrets.token_hex(16)
        )
        store = Store(root, binding, create=True)
        try:
            store.evidence(Evidence(1, binding, "exempt", None, None, (), 0, verify_record.pointer(found)))
        finally:
            store.close()
        test_select.publish(current, selection, head_sha=tree.head, observed_at=iso(now()))
        publish(root, current, binding, "record", event_id)
    return True
