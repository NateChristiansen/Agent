"""Keep post-land bookkeeping as durable obligations on the prepared journal. Pending milestone and
manager notices survive every transition, including a crash just after done. Reconcile after
landing and recovery without replaying Git or raising.

A landing is not task acceptance, so notify the launching manager and live local feature managers
about unticked work rather than tick it. Messages request relay for remote ownership. Aborted
journals owe nothing; legacy journals without obligation fields are not backfilled.

The `cleanup` obligation removes the landed dispatches' worktrees through dispatch cleanup's
policy, never inside the transaction and never from `reconcile`: `retry_cleanup` settles it under
the land flock after a landing and before an allocation. A held candidate stays on the journal with
its reasons, and the obligation stays pending until every candidate is removed, gone or owned by a
later landing, with no age limit.

The `reconcile` obligation registers the landing's acceptance sources in the feature ledger: the
sources are saved on the journal first, then appended once each. Journals prepared without the
obligation register nothing.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import land_journal as jn
from .core import AgentError
from .reconcile import INTENT, OBLIGATION
from .records.narrow import as_dict, as_list

PENDING = "pending"
CLEANUP = "cleanup"
RECONCILE = OBLIGATION
# The `reconcile` obligation before its sources are saved on the journal. Only this module moves
# it on, so the ledger's heal, which settles a `pending` obligation from the saved sources, never
# settles a landing whose sources were not yet worked out.
RECORDING = "recording"
SETTLED = ("removed", "gone", "superseded")
RETRY_LIMIT = 8


def initial() -> dict[str, str]:
    """The obligations a prepared journal starts with."""
    return {"milestone": PENDING, "notices": PENDING, CLEANUP: PENDING, RECONCILE: RECORDING}


def outstanding(journal: dict[str, Any]) -> bool:
    """A successful landing that still owes its metadata. Aborted and legacy journals never do;
    cleanup is `cleanup_owed`, which recovery's metadata pass does not replay."""
    obligations = {k: v for k, v in as_dict(journal.get("obligations")).items() if k != CLEANUP}
    return journal.get("state") == "done" and bool({PENDING, RECORDING} & set(obligations.values()))


def cleanup_owed(journal: dict[str, Any]) -> bool:
    """A successful landing whose worktree cleanup is still pending. Only journals prepared with
    the obligation owe it: older ones are never backfilled into destructive work."""
    return journal.get("state") == "done" and as_dict(journal.get("obligations")).get(CLEANUP) == PENDING


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
    stored = as_dict(journal.get("obligations"))
    obligations = {"milestone": PENDING, "notices": PENDING, **stored} if "obligations" in journal else {}
    if not obligations or journal.get("state") != "done":
        return obligations
    try:
        if obligations["milestone"] == PENDING and _milestone(root, journal):
            obligations["milestone"] = "recorded"
        if obligations["notices"] == PENDING and _notices(root, journal):
            obligations["notices"] = "done"
    except Exception:  # noqa: BLE001, S110 — whatever is still pending is retried by recovery
        pass
    try:
        if obligations.get(RECONCILE) in (RECORDING, PENDING) and _sources(root, main, journal, obligations):
            obligations[RECONCILE] = "done"
    except Exception:  # noqa: BLE001, S110 — the saved sources stay pending for recovery or the heal
        pass
    if obligations != journal.get("obligations"):
        journal["obligations"] = obligations
        try:
            jn.write(main, journal)
        except OSError:
            pass
    return obligations


def _sources(root: Path, main: Path, journal: dict[str, Any], obligations: dict[str, Any]) -> bool:
    """Work out the landing's ledger sources and save them on the journal before appending any
    (`recording` becomes `pending`), then append each once. True when every one is in its
    feature's log; a crash after the save leaves the ledger's heal to finish them."""
    from . import features as features_mod
    from . import reconcile as reconcile_mod

    if obligations[RECONCILE] == RECORDING:
        journal[INTENT] = landing_intents(root, journal)
        obligations[RECONCILE] = PENDING
        jn.write(main, {**journal, "obligations": dict(obligations)})
    complete = True
    for intent in (dict(as_dict(p)) for p in as_list(journal.get(INTENT))):
        try:
            reconcile_mod.append_source(features_mod.resolve(root, str(intent["feature"])), intent)
        except AgentError, OSError, KeyError:
            complete = False
    return complete


def landing_intents(root: Path, journal: dict[str, Any]) -> list[dict[str, Any]]:
    """One `accept:` source per landed dispatch with open listed tasks, and one `landing:` source
    per feature owning a member branch that has no dispatch record. An unbound branch raises none."""
    from . import features as features_mod
    from . import reconcile as reconcile_mod
    from . import verify_record
    from .activity_gate import bound_feature
    from .dispatch import state as dispatch_state

    ident = str(journal["id"])
    branches = [b for b in _branches(journal) if b]
    harness = verify_record.harness_repo(root).name
    records = [
        d for d in dispatch_state.load_all(root) if d.get("branch") in branches and d.get("repo") == harness
    ]
    out: list[dict[str, Any]] = []
    for branch in branches:
        landed = [
            d
            for d in records
            if d.get("branch") == branch and not newer_mark(root, as_dict(d.get("integration")), journal)
        ]
        if not landed:
            feature = bound_feature(root, branch)
            if feature is not None:
                out.append(reconcile_mod.landing_source(ident, feature))
            continue
        for d in landed:
            try:
                feature = features_mod.resolve(root, str(d["feature"]))
            except AgentError, KeyError:
                continue  # its feature no longer resolves: nothing to accept against
            if (accept := reconcile_mod.accept_source(d, ident, feature=feature)) is not None:
                out.append(accept)
    unique = {json.dumps(s, sort_keys=True): s for s in out}
    return list(unique.values())


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


def retry_cleanup(
    root: Path,
    *,
    current: dict[str, Any] | None = None,
    keep: tuple[str, ...] = (),
    main: Path | None = None,
    harness: str | None = None,
) -> dict[str, Any] | None:
    """Settle the cleanup that done landings still owe, least recently tried first and at most
    `RETRY_LIMIT` of them. Takes the land flock without waiting: a landing that holds it retries
    them itself. `current` is a journal the caller holds, settled in place; `keep` are worktree
    paths about to be reused, left for a later try. `main` and `harness` are the main checkout
    and harness repo name when the caller already has them; otherwise they are resolved here.
    Never raises; returns `current`."""
    from .dispatch.allocation import worktree_key

    try:
        if main is None:
            from .land import main_of

            main = main_of(root)
        if current is None and not any(cleanup_owed(j) for j in jn.every(main)):
            return None
        with jn.flock(main):
            owed = [j for j in jn.every(main) if cleanup_owed(j)]
            if current is not None:
                owed = [current if j["id"] == current["id"] else j for j in owed]
            owed.sort(key=lambda j: str(as_dict(j.get("cleanup")).get("tried") or ""))
            kept = {worktree_key(p) for p in keep}
            for journal in owed[:RETRY_LIMIT]:
                settle_cleanup(root, main, journal, keep=kept, harness=harness)
    except Exception:  # noqa: BLE001, S110 — the landing stands; what is owed stays pending
        pass
    return current


def settle_cleanup(
    root: Path,
    main: Path,
    journal: dict[str, Any],
    *,
    keep: set[str] | None = None,
    harness: str | None = None,
) -> None:
    """One cleanup attempt for each of the journal's unsettled candidates, then save what each
    one is now. The caller holds the land flock. A failure is recorded on the journal."""
    from .dispatch.allocation import worktree_key

    record = as_dict(journal.get("cleanup"))
    rows = {str(as_dict(r).get("id")): as_dict(r) for r in as_list(record.get("candidates"))}
    error = ""
    skipped = False
    try:
        seen: set[str] = set()
        for d in _cleanup_candidates(root, journal, harness=harness):
            ident = str(d["id"])
            seen.add(ident)
            if rows.get(ident, {}).get("state") in SETTLED:
                continue
            if worktree_key(str(d["worktree"])) in (keep or set()):
                skipped = True
                continue
            rows[ident] = _clean(root, journal, d)
        for ident, row in rows.items():
            if ident not in seen and row.get("state") not in SETTLED:
                rows[ident] = {**row, "state": "gone", "holds": []}
    except Exception as exc:  # noqa: BLE001 — recorded, and retried at the next opportunity
        error = f"cleanup failed: {exc}"
    journal["cleanup"] = {
        "tried": jn.now(),
        "candidates": list(rows.values()),
        **({"error": error} if error else {}),
    }
    if not error and not skipped and all(r.get("state") in SETTLED for r in rows.values()):
        journal["obligations"] = {**as_dict(journal.get("obligations")), CLEANUP: "done"}
    try:
        jn.write(main, journal)
    except OSError:
        pass


def held(journal: dict[str, Any]) -> list[dict[str, Any]]:
    """The journal's cleanup candidates still kept, with their reasons."""
    rows = as_list(as_dict(journal.get("cleanup")).get("candidates"))
    return [as_dict(r) for r in rows if as_dict(r).get("state") not in SETTLED]


def cleanup_text(journal: dict[str, Any]) -> str:
    """One line on the journal's worktree cleanup, or "" when it owes none."""
    obligation = as_dict(journal.get("obligations")).get(CLEANUP)
    if obligation is None:
        return ""
    record = as_dict(journal.get("cleanup"))
    rows = [as_dict(r) for r in as_list(record.get("candidates"))]
    removed = [r for r in rows if r.get("state") == "removed"]
    kept = held(journal)
    if obligation == PENDING and not record:
        return "cleanup: queued; it runs at the next landing or dispatch allocation"
    text = f"cleanup: removed {len(removed)}, held {len(kept)}"
    if kept:
        text += ": " + "; ".join(
            f"{r.get('worktree')} [{r.get('id')}] ({', '.join(str(h) for h in as_list(r.get('holds')))})"
            for r in kept
        )
    if record.get("error"):
        text += f"; {record['error']}"
    if obligation == PENDING:
        text += "; retried at the next landing or dispatch allocation"
    return text


def _cleanup_candidates(
    root: Path, journal: dict[str, Any], *, harness: str | None = None
) -> list[dict[str, Any]]:
    """The harness dispatches with a worktree on a branch this journal landed. The harness repo
    is looked up only when some dispatch is on one of those branches."""
    from . import verify_record
    from .dispatch import state as dispatch_state

    branches = set(_branches(journal))
    on_branch = [
        d for d in dispatch_state.load_all(root) if d.get("branch") in branches and d.get("worktree")
    ]
    if not on_branch:
        return []
    if harness is None:
        harness = verify_record.harness_repo(root).name
    return [d for d in on_branch if d.get("repo") == harness]


def _landed_tip(journal: dict[str, Any], branch: str) -> str:
    members = as_list(journal.get("members")) or [
        {"branch": journal.get("branch"), "tip": journal.get("tip")}
    ]
    for member in members:
        if str(as_dict(member).get("branch") or "").removeprefix("refs/heads/") == branch:
            return str(as_dict(member).get("tip") or "")
    return ""


def _clean(root: Path, journal: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    """Apply dispatch cleanup to one candidate, rechecked first against this landing's exact tip."""
    from . import repos as repos_mod
    from . import tidy
    from .dispatch import state as dispatch_state
    from .dispatch.worktrees import cleanup

    row: dict[str, Any] = {"id": d["id"], "worktree": str(Path(d["worktree"])), "branch": d["branch"]}

    def keep(*reasons: str) -> dict[str, Any]:
        return {**row, "state": "held", "holds": list(reasons)}

    integration = as_dict(d.get("integration"))
    if newer_mark(root, integration, journal):
        return {**row, "state": "superseded", "holds": []}  # the later landing owns its cleanup
    if d.get("worktree_removed"):
        return {**row, "state": "gone", "holds": []}
    if integration.get("journal") != journal["id"]:
        return keep(f"not marked landed by {journal['id']} yet")
    landed = _landed_tip(journal, str(d["branch"])) or str(integration.get("tip") or "")
    rp = repos_mod.load_registry(root).find(d["repo"])
    if rp is None:
        return keep(f"repo {d['repo']} is not registered")
    tip = tidy.branch_tip(rp.abs_path(root), str(d["branch"]))
    if not landed or tip != landed:
        return keep(
            f"{d['branch']} is at {tip[:12] or 'no commit'}, not the landed {landed[:12] or 'tip'}; "
            "kept until that exact head lands"
        )
    try:
        result = cleanup(root, dispatch_id=str(d["id"]))
    except AgentError as exc:
        return keep(str(exc))
    except Exception as exc:  # noqa: BLE001 — recorded on the candidate; the landing stands
        return keep(f"cleanup failed: {exc}")
    if row["worktree"] in as_list(result.get("removed")):
        return {**row, "state": "removed", "holds": []}
    reasons = [
        str(as_dict(r).get("reason"))
        for key in ("skipped", "blocked", "missing")
        for r in as_list(result.get(key))
    ]
    if reasons:
        return keep(*reasons)
    if dispatch_state.load(root, str(d["id"])).get("worktree_removed"):
        return {**row, "state": "gone", "holds": []}
    return keep("cleanup removed nothing; retried later")


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
