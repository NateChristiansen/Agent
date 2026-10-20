"""The two ways the owner closes a finished dispatch: `abandon` (nobody will
resume it) and `ack` (dealt with, state unchanged). Both set `actioned_at`, which is what takes
a dispatch off the Workers view and the session-start block (`state.is_live`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import features as features_mod
from .. import log as log_mod
from ..core import AgentError, iso, now
from . import holdings, notice_producers, telemetry_events
from .handoffs import write_handoff
from .state import CLOSED_STATES, load, render, save
from .verification_gate import record_guard, requires_lease


def _mark_actioned(
    root: Path, dispatch_id: str, *, action: str, note: str | None, by: str = "owner"
) -> dict[str, Any]:
    d = load(root, dispatch_id)
    if requires_lease(root, d):
        raise AgentError("dispatch cleanup is incomplete; collect it before closing")
    if d["state"] in ("queued", "running"):
        raise AgentError(f"dispatch {dispatch_id} is {d['state']}; cancel it first")
    stamp = iso(now())
    d["actioned_at"] = stamp
    d["actioned"] = {"action": action, "note": (note or "").strip() or None}
    d["chat_pending"] = False
    text = action if not d["actioned"]["note"] else f"{action}: {d['actioned']['note']}"
    # Record MCP actions as the manager's and CLI actions as the owner's.
    d.setdefault("thread", []).append({"ts": stamp, "from": by, "text": text})
    return d


def mark_landed(d: dict[str, Any], journal: dict[str, Any]) -> None:
    """Landing is the third way a finished dispatch is dealt with: before it, only
    `ack` or `abandon` cleared one, so a landed worker kept its manager's tab marked. The stamp is
    the landing's start as its id records it (`20261011T170000-…`, UTC), never now and never the
    journal's `ts`, which each later transition and a recovery rewrite: an attempt that finished
    after the landing began (a resume) still waits, and recovering an old journal cannot move the
    stamp past it. A running or already actioned dispatch is
    left alone."""
    from datetime import UTC, datetime

    from .state import is_actioned

    if d.get("state") in ("queued", "running") or is_actioned(d):
        return
    try:
        started = datetime.strptime(str(journal["id"])[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except KeyError, ValueError:
        return
    stamp = iso(started.astimezone(now().tzinfo))
    d["actioned_at"] = stamp
    d["actioned"] = {"action": "landed", "note": str(journal.get("id") or "")}
    if is_actioned(d):
        # The landed branch is resolved, and landing it read it. A shell landing has
        # no session, so it records the landing user. An attempt that finished after the landing
        # began is not what landed and stays unread.
        _landing_read(d, journal)


def _landing_read(d: dict[str, Any], journal: dict[str, Any]) -> None:
    from . import verdicts

    d["chat_pending"] = False
    notice_producers.acknowledge(d)
    if verdicts.has_verdict(d):
        verdicts.mark(d, verdicts.landed_by(journal.get("session")), "land")


def read_landed_reviews(root: Path, journal: dict[str, Any]) -> list[str]:
    """Write verdict-read for the reviewer dispatches of the landed features whose verdict is
    linked to the landed artifact: a read-only review (`writes=[]` or row `review`)
    of the same feature, finished before the landing began, that reviewed a landed branch by name
    (its `base`, or its work order naming the branch) or a commit the landing brought in (its
    `start_sha` between the old main and a landed tip). Returns the ids marked. Best effort, like
    every landing follow-up: a record that cannot be read or saved is skipped."""
    from datetime import UTC, datetime

    from ..records.narrow import dig
    from . import verdicts
    from .prompt import manager_prompt
    from .state import is_actioned, load_all

    try:
        started = datetime.strptime(str(journal["id"])[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except KeyError, ValueError:
        return []
    members = _members(journal)
    branches = {b for b, _ in members if b}
    records = load_all(root)
    features = {
        d.get("feature") for d in records if d.get("branch") in branches and d.get("feature") is not None
    }
    marked: list[str] = []
    for d in records:
        if d.get("feature") not in features or d.get("branch") in branches:
            continue
        if not (d.get("writes") == [] or d.get("row") == "review") or not verdicts.has_verdict(d):
            continue
        if is_actioned(d) or dig(d, verdicts.FIELD, "key") == verdicts.key(d):
            continue
        try:
            finished = datetime.fromisoformat(str(d.get("finished")))
        except ValueError:
            continue
        if finished.tzinfo is None or finished > started:
            continue
        prompt = manager_prompt(d) or ""
        linked = (
            d.get("base") in branches
            or any(b in prompt for b in branches)
            or _reviewed_landed_commit(root, d, journal, members)
        )
        if not linked:
            continue
        try:
            with record_guard(root, str(d["id"])) as current:
                if verdicts.has_verdict(current) and current.get("attempt") == d.get("attempt"):
                    verdicts.mark(current, verdicts.landed_by(journal.get("session")), "land")
                    current["chat_pending"] = False
                    save(root, current)
                    marked.append(str(d["id"]))
        except AgentError, OSError:
            continue
    return marked


def _members(journal: dict[str, Any]) -> list[tuple[str, str]]:
    """(branch, tip) for each branch a landing journal landed: a batch's members, or its one."""
    from ..records.narrow import as_dict, as_list

    rows = as_list(journal.get("members")) or [{"branch": journal.get("branch"), "tip": journal.get("tip")}]
    out: list[tuple[str, str]] = []
    for row in rows:
        member = as_dict(row)
        out.append(
            (str(member.get("branch") or "").removeprefix("refs/heads/"), str(member.get("tip") or ""))
        )
    return out


def _reviewed_landed_commit(
    root: Path, d: dict[str, Any], journal: dict[str, Any], members: list[tuple[str, str]]
) -> bool:
    """Whether the reviewer started from a commit this landing brought onto main: an ancestor of
    a landed tip that the old main did not already hold. A reviewer started from main reviewed
    main, not the branch."""
    import subprocess

    start, old = str(d.get("start_sha") or ""), str(journal.get("old") or "")
    if not start or not old:
        return False
    try:
        from ..land import main_of

        main = str(main_of(root))

        def ancestor(a: str, b: str) -> bool:
            proc = subprocess.run(  # noqa: S603 — fixed git argv, SHAs from our own records
                ["git", "-C", main, "merge-base", "--is-ancestor", a, b],  # noqa: S607
                capture_output=True,
                timeout=10,
                check=False,
            )
            return proc.returncode == 0

        if ancestor(start, old):
            return False
        return any(tip and ancestor(start, tip) for _, tip in members)
    except OSError, subprocess.SubprocessError, AgentError:
        return False


def abandon(root: Path, *, dispatch_id: str, note: str, by: str = "owner") -> dict[str, Any]:
    """Close a finished dispatch nobody will resume: a stale question, a superseded blocker, a
    probe whose return was the result. Terminal `abandoned`; worktree and branch are kept. The
    reason is required, because it is the only record of why the ask died."""
    if not note or not note.strip():
        raise AgentError("abandon needs a --note: one line on why nobody will resume it")
    with record_guard(root, dispatch_id):
        d = _mark_actioned(root, dispatch_id, action="abandoned", note=note, by=by)
        if d["state"] == "abandoned":
            raise AgentError(f"dispatch {dispatch_id} is already abandoned")
        holdings.abandon(root, d)
        d["abandoned_from"] = d["state"]
        d["state"] = "abandoned"
        notice_producers.supersede(d, "abandon")
        save(root, d)
        feat = features_mod.resolve(root, str(d["feature"]))
        write_handoff(root, d, feat)
        log_mod.append(
            feat,
            "note",
            f"dispatch {d['id']} ({d['short']}) abandoned from {d['abandoned_from']}: {note.strip()}",
        )
        render(root)
        return d


def ack(root: Path, *, dispatch_id: str, note: str | None = None, by: str = "owner") -> dict[str, Any]:
    """The owner has dealt with a finished dispatch (pushed it, read it, nothing to do), so it
    leaves the Workers view and the session-start block. State is unchanged. A later resume
    finishes after this mark and brings it back."""
    with record_guard(root, dispatch_id):
        d = _mark_actioned(root, dispatch_id, action="ack", note=note, by=by)
        if d["state"] in CLOSED_STATES:
            raise AgentError(f"dispatch {dispatch_id} is {d['state']}; nothing waits on it")
        notice_producers.acknowledge(d)
        save(root, d)
        telemetry_events.ack(root, d)
        feat = features_mod.resolve(root, str(d["feature"]))
        write_handoff(root, d, feat)
        render(root)
        return d
