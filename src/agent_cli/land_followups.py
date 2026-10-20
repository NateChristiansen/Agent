"""Keep post-land bookkeeping as durable obligations on the prepared journal. Pending milestone and
manager notices survive every transition, including a crash just after done. Reconcile after
landing and recovery without replaying Git or raising.

A landing is not task acceptance, so notify the launching manager and live local feature managers
about unticked work rather than tick it. Messages request relay for remote ownership. Aborted
journals owe nothing; legacy journals without obligation fields are not backfilled.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import land_journal as jn
from .core import AgentError
from .records.narrow import as_dict, as_list

PENDING = "pending"


def initial() -> dict[str, str]:
    """The obligations a prepared journal starts with."""
    return {"milestone": PENDING, "notices": PENDING}


def outstanding(journal: dict[str, Any]) -> bool:
    """A successful landing that still owes something. Aborted and legacy journals never do."""
    obligations = as_dict(journal.get("obligations"))
    return journal.get("state") == "done" and PENDING in obligations.values()


def reconcile(root: Path, main: Path, journal: dict[str, Any]) -> dict[str, Any]:
    """Mark the landed dispatches, then settle what is still owed, and save the obligations.
    Metadata only; never raises. Returns the obligations as they now stand."""
    from .land import note_members

    try:
        note_members(root, journal)
    except Exception:  # noqa: BLE001, S110 — the landing is done; integration marks are best effort
        pass
    if journal.get("state") == "done":
        try:
            from .dispatch.closing import read_landed_reviews

            read_landed_reviews(root, journal)  # their verdicts were read for this landing
        except Exception:  # noqa: BLE001, S110 — best effort, like the integration marks
            pass
    obligations = {**initial(), **as_dict(journal.get("obligations"))} if "obligations" in journal else {}
    if not obligations or journal.get("state") != "done":
        return obligations
    try:
        if obligations["milestone"] == PENDING and _milestone(root, journal):
            obligations["milestone"] = "recorded"
        if obligations["notices"] == PENDING and _notices(root, journal):
            obligations["notices"] = "done"
    except Exception:  # noqa: BLE001, S110 — whatever is still pending is retried by recovery
        pass
    if obligations != journal.get("obligations"):
        journal["obligations"] = obligations
        try:
            jn.write(main, journal)
        except OSError:
            pass
    return obligations


def newer_mark(root: Path, held: dict[str, Any], journal: dict[str, Any]) -> bool:
    """Whether a dispatch's integration mark `held` comes from a later landing than `journal`.

    Main moves forward only, so a later landing's main descends from an earlier one's candidate:
    Git ancestry decides, which a read answers without replaying anything. Landing ids sort by
    second and then by a random suffix, so they cannot order two landings in the same second;
    they are only the fallback when Git cannot answer."""
    held_journal, held_main = str(held.get("journal") or ""), str(held.get("main") or "")
    if not held_journal or held_journal == journal["id"]:
        return False
    cand = str(journal.get("cand") or "")
    if held_main and cand and held_main != cand:
        import subprocess

        try:
            from .land import main_of

            proc = subprocess.run(  # noqa: S603 — fixed git argv, two SHAs from our own records
                ["git", "-C", str(main_of(root)), "merge-base", "--is-ancestor", cand, held_main],  # noqa: S607
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc.returncode in (0, 1):
                return proc.returncode == 0
        except OSError, subprocess.SubprocessError, AgentError:
            pass
    return held_journal > str(journal["id"])


def _day(journal: dict[str, Any]) -> date:
    try:
        return datetime.fromisoformat(str(journal.get("ts"))).date()
    except ValueError:
        from .core import today

        return today()


def _branches(journal: dict[str, Any]) -> list[str]:
    members = as_list(journal.get("members")) or [{"branch": journal.get("branch")}]
    return [str(as_dict(m).get("branch") or "").removeprefix("refs/heads/") for m in members]


def _milestone(root: Path, journal: dict[str, Any]) -> bool:
    from .journal import pending

    detail = ", ".join(b for b in _branches(journal) if b)
    ident = str(journal["id"])
    return pending.record(root, "landing", ident, detail, day=_day(journal), key=ident)


def _notices(root: Path, journal: dict[str, Any]) -> bool:
    """Deliver every owed notice for this landing; True once each one is complete."""
    from . import verify_record
    from .dispatch import state as dispatch_state

    harness = verify_record.harness_repo(root).name
    branches = set(_branches(journal))
    complete = True
    for d in dispatch_state.load_all(root):
        if d.get("branch") not in branches or d.get("repo") != harness:
            continue
        try:
            current = dispatch_state.load(root, str(d["id"]))
            integration = as_dict(current.get("integration"))
            landed_by = str(integration.get("journal") or "")
            if newer_mark(root, integration, journal):
                continue  # landed again since: that landing owns the notice
            if landed_by != journal["id"]:
                complete = False  # its integration mark did not land yet: retry, never drop it
                continue
            try:
                open_tasks = _unticked(root, current)
            except AgentError:
                continue  # its feature no longer resolves: nobody to tell, and the others go on
            if not open_tasks:
                continue
            notices = dict(as_dict(integration.get("notices")))
            for recipient in _recipients(root, current):
                done = _deliver(
                    root, current, journal, open_tasks, recipient, as_dict(notices.get(recipient))
                )
                notices[recipient] = done
                complete = complete and _complete(done)
            current["integration"] = {**integration, "notices": notices}
            dispatch_state.save(root, current)
        except AgentError, OSError:
            complete = False  # this member is retried; the batch's others are not held back
    return complete


def _unticked(root: Path, d: dict[str, Any]) -> list[int]:
    from . import features as features_mod

    wanted = {int(t) for t in as_list(d.get("tasks")) if isinstance(t, int)}
    if not wanted or d.get("feature") is None:
        return []
    f = features_mod.resolve(root, str(d["feature"]))
    return [t.index for t in features_mod.list_tasks(f.body) if t.index in wanted and t.is_open]


def _live_managers(root: Path) -> list[dict[str, Any]]:
    """Manager records whose process is alive, from the records alone.

    Not `ops.peers.addressable_peers`: that also scans the process table for each dead-looking
    record to revive a resumed session, which took about 6.5 s on the owner's machine. A resumed session this misses gets noticed on its next prompt. Moved here from
    the retired `pairing.py`, whose plan gate was its other caller."""
    from . import sessions
    from .core import is_alive
    from .records.narrow import dig
    from .session_liveness import record_live

    return [
        r
        for r in sessions.all_records(root)
        if not r.get("dispatch_id") and record_live(r, is_alive) and dig(r, "activity", "state") != "ended"
    ]


def _recipients(root: Path, d: dict[str, Any]) -> list[str]:
    """The launching manager, then the live local managers mapped to the feature."""
    out: list[str] = []
    launcher = str(d.get("launched_by_session") or "")
    if launcher:
        out.append(launcher)
    for record in _live_managers(root):
        sid = str(record.get("session_id") or "")
        if sid and sid not in out and as_dict(record.get("feature")).get("id") == d.get("feature"):
            out.append(sid)
    return out


def _deliver(
    root: Path,
    d: dict[str, Any],
    journal: dict[str, Any],
    tasks: list[int],
    recipient: str,
    prior: dict[str, Any],
) -> dict[str, Any]:
    from .ops.peer_delivery import deliver

    number = f"{d['feature']:03d}" if isinstance(d.get("feature"), int) else str(d.get("feature"))
    listed = ", ".join(f"#{t}" for t in tasks)
    message = (
        f"{number} task(s) {listed} landed on main in `{str(journal.get('cand'))[:12]}` (`{d['id']}`). "
        f"Tick with `task_done` if accepted, or record what acceptance remains. "
        f"If a manager on another machine owns {number}, tell it."
    )
    # Sent as the session that asked for the landing when it is known, so its own notice is
    # queued for its next turn rather than rung (`peer_delivery.SELF`).
    sender = str(journal.get("session") or "agent-land")
    try:
        delivery = deliver(root, target=recipient, frm=sender, message=message, notice=prior or None)
    except (AgentError, OSError) as exc:
        return {"to": recipient, "id": prior.get("id"), "wake_id": None, "rang": "failed", "detail": str(exc)}
    return {**delivery.notice(), "detail": delivery.detail}


def _complete(notice: dict[str, Any]) -> bool:
    """`peer_delivery.Delivery.complete` for a stored notice: rung, covered by an accepted
    wake, read, or queued for the session that asked for the landing."""
    from .ops.peer_delivery import COMPLETE, SELF

    return notice.get("rang") in COMPLETE or (notice.get("rang") == "unrung" and notice.get("detail") == SELF)
