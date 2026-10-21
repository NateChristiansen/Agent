"""Build optional session-start context for tasks, peers, uncommitted source, retros, unassigned
captures and journal work. Builders return empty text when quiet and are not hook entry points.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .. import capture as capture_mod
from .. import sessions
from ..core import is_alive
from ..records.narrow import dig
from ..session_liveness import record_live


def full_pass_block(root: Path) -> str:
    """Push certification is actionable on the source machine; Windows is a preview."""
    import sys

    from .. import git_discovery, harness_role, verify_record
    from ..core import agent_path

    windows = sys.platform == "win32"
    if not windows and harness_role.consumer(root):
        return ""
    if windows:
        marker = agent_path(root, "land", "full-pass-preview-noted")
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            with marker.open("x", encoding="utf-8") as stream:
                stream.write("Windows preview; pushes are certified on the source machine.\n")
        except FileExistsError:
            return ""
        return "INFO: Windows is a preview; the source machine certifies pushes with a full pass."
    try:
        if git_discovery.common_dir(root) is None:
            return ""
    except Exception:  # noqa: BLE001 — a fresh install has no main to certify
        return ""
    try:
        certified = verify_record.lookup(root, "refs/heads/main")[0] is not None
    except Exception:  # noqa: BLE001 — session start still reports missing evidence
        certified = False
    return (
        ""
        if certified
        else ("WARNING: main's tip has no full pass record; run `agent verify record main` before pushing")
    )


OPEN_TASKS_SHOWN = 8
OPEN_TASK_CHARS = 120


def open_tasks_block(root: Path, feature: dict[str, Any], limit: int | None = OPEN_TASKS_SHOWN) -> str:
    """The mapped feature's unchecked tasks. Every session starts by seeing what is still open.
    Ticking is the `task_done` tool."""
    try:
        from .. import features as features_mod
        from .. import maintenance

        f = features_mod.resolve(root, str(feature["id"]))
        if f"{f.id:03d}" in maintenance.parked(root):
            return (
                f"\nfeature {f.id:03d} is parked (maintenance): no dispatch or resume until the owner "
                "restarts it per its plan's restart note."
            )
        tasks = [t for t in features_mod.list_tasks(f.body) if t.is_open]
        if f.is_request:
            return _request_block(f, tasks)
    except Exception:  # noqa: BLE001
        return ""
    if not tasks:
        return "\nopen tasks: none. Add one with the task_add tool before starting new work."
    lines = [
        f"\nopen tasks ({len(tasks)}) — tick with the task_done tool when you finish one, before you report it done:"
    ]
    shown = tasks if limit is None else tasks[:limit]
    for t in shown:
        text = (
            t.text if limit is None or len(t.text) <= OPEN_TASK_CHARS else t.text[: OPEN_TASK_CHARS - 1] + "…"
        )
        lines.append(f"  #{t.index} {text}")
    if len(shown) < len(tasks):
        lines.append(
            f"  … {len(tasks) - len(shown)} more: task_list with open_only=true, or the full-list file"
        )
    return "\n".join(lines)


def _request_block(f: Any, tasks: list[Any]) -> str:
    """A mapped request's ask, status and next action instead of a task prompt: its contract is
    the ask and the result, and tasks are optional."""
    from .. import requests as requests_mod

    lines = [
        f"\n{f.label} [{f.status}] {f.meta.get('category') or ''}: {requests_mod.short(requests_mod.ask_of(f), OPEN_TASK_CHARS)}",
        f"next: {requests_mod.next_action(f)} (`request` tool, action `complete`, with the result)",
    ]
    lines += [f"  open task #{t.index} {t.text[:OPEN_TASK_CHARS]}" for t in tasks]
    return "\n".join(lines)


def other_sessions_block(root: Path, session_id: str) -> str:
    """Item 5: every OTHER live session, with its feature and what it holds.

    The manager sees who else is in the vault before it starts editing (harness/sessions.md's
    awareness section). Liveness is the record's own `pid`. A dispatched worker's session
    record does not carry one; its liveness lives on the dispatch record instead, which is
    `session list`'s concern, not this awareness line's. So a worker never appears here."""
    from .. import leases as leases_mod

    others = [
        r
        for r in sessions.all_records(root)
        if r.get("session_id")
        and r.get("session_id") != session_id
        and not r.get("dispatch_id")
        and dig(r, "activity", "state") != "ended"
        and record_live(r, is_alive)
    ]
    unverified = _unverified_lines(root, session_id, others, leases_mod)
    if not others:
        return "## Other live sessions\n" + "\n".join(unverified) if unverified else "no other live sessions"
    live_leases = [
        lease for lease in leases_mod.list_all(root) if lease.get("status") in leases_mod.BLOCKING_STATUSES
    ]
    lines = ["## Other live sessions"]
    from .. import hook_freshness

    fresh_cache: dict[str, datetime | None] = {}
    for r in sorted(others, key=lambda r: str(r.get("name") or r.get("session_id"))):
        label = str(r.get("name") or r.get("session_id"))
        feature = r.get("feature")
        feat_label = f"feature {int(feature['id']):03d} {feature['slug']}" if feature else "no feature"
        held = [
            f"{lease['area']} (touched {leases_mod.minutes_ago(lease.get('touched_at'))}m ago)"
            for lease in live_leases
            if lease.get("session_id") == r.get("session_id")
        ]
        holds = "holds " + ", ".join(held) if held else "holds nothing"
        stale = hook_freshness.mark(root, r, fresh_cache)
        lines.append(f"{label} — {feat_label}, {holds}" + (f", {stale}" if stale else ""))
    return "\n".join(lines + unverified)


UNVERIFIED_HIDE_MINUTES = 24 * 60


def _quiet_age(minutes: int) -> str:
    return f"{minutes // 60}h" if minutes >= 60 else f"{minutes}m"


def _unverified_lines(root: Path, session_id: str, live: list[dict[str, Any]], leases_mod: Any) -> list[str]:
    """Quiet unknown-pid Codex managers past the 30-minute rule, up to a day: named, not
    vouched for. Total: a failure here leaves the live lines alone."""
    try:
        from ..ops.peers import quiet_codex
        from ..session_liveness import last_seen

        live_ids = {r.get("session_id") for r in live}
        lines: list[str] = []
        for r in sorted(quiet_codex(root), key=lambda r: str(r.get("name") or r.get("session_id"))):
            if (
                r.get("session_id") == session_id
                or r.get("session_id") in live_ids
                or record_live(r, is_alive)
            ):
                continue
            at = last_seen(r)
            if not at:
                continue
            minutes = leases_mod.minutes_ago(at)
            if minutes >= UNVERIFIED_HIDE_MINUTES:
                continue
            feature = r.get("feature")
            feat_label = f"feature {int(feature['id']):03d} {feature['slug']}" if feature else "no feature"
            lines.append(
                f"unverified (quiet {_quiet_age(minutes)}): {r.get('name') or r.get('session_id')} — {feat_label}"
            )
        return lines
    except Exception:  # noqa: BLE001
        return []


def dirty_src_warning(root: Path) -> str:
    """Show uncommitted source and tests so concurrent managers can see unfinished edits. Bound git
    status to two seconds so slow Git cannot hold session-start.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "tests"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    if proc.returncode != 0:
        return ""
    files = [line for line in proc.stdout.splitlines() if line.strip()]
    if not files:
        return ""
    return f"uncommitted changes under src/ or tests/: {len(files)} file(s)"


DISPATCHES_SINCE_RETRO_THRESHOLD = 5


def lesson_nag(root: Path) -> str:
    """Keep lessons, finished-feature closure and dispatch cadence cues at session start."""
    from .. import features as features_mod

    lines: list[str] = []
    try:
        feats = features_mod.load_all(root)
    except Exception:  # noqa: BLE001
        feats = []
    for f in feats:
        if f.is_request or f.status != "active" or (f.dir / "retro.md").exists():
            continue
        tasks = f.tasks()
        if tasks["done"] and not tasks["open"]:
            lines.append(f"finished, not closed: {f.number} — run /retro then /close")
    n = dispatches_since_last_retro(root)
    if n >= DISPATCHES_SINCE_RETRO_THRESHOLD:
        lines.append(f"{n} dispatches since the last retro")
    try:
        from .. import lessons

        queued = lessons.nudge(root)
    except Exception:  # noqa: BLE001 — a reporting failure must not stop session start
        queued = None
    if queued:
        lines.append(queued)
    return "\n".join(lines)


def dispatches_since_last_retro(root: Path) -> int:
    """Count real dispatch records newer than the last `retro.md`, or all if none exists. A
    request needs no retro, so its dispatches are not counted."""
    from .. import dispatch as dispatch_mod
    from .. import features as features_mod

    dispatches_dir = dispatch_mod.dispatches_dir(root)
    if not dispatches_dir.exists():
        return 0
    skip: set[int] = set()
    try:
        skip = {f.id for f in features_mod.load_all(root) if f.is_request}
    except Exception:  # noqa: BLE001, S110 — an unreadable store counts every dispatch, as before
        pass
    files = [
        p
        for p in dispatches_dir.glob("*.json")
        if (m := dispatch_mod.ID_RE.match(p.stem)) and int(m.group("feature")) not in skip
    ]
    if not files:
        return 0
    retros = list((root / "features").glob("*/retro.md"))
    if not retros:
        return len(files)
    last_retro = max(p.stat().st_mtime for p in retros)
    return sum(1 for p in files if p.stat().st_mtime > last_retro)


UNASSIGNED_HEAVY_RECORDS = 20


def unassigned_days(root: Path) -> list[tuple[str, int]]:
    """(day, record count) for each heavy day under `.agent/raw/unassigned/`, oldest first."""
    base = root / capture_mod.UNASSIGNED
    if not base.exists():
        return []
    days: list[tuple[str, int]] = []
    for path in sorted(base.glob("*.jsonl")):
        try:
            count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            continue
        if count > UNASSIGNED_HEAVY_RECORDS:
            days.append((path.stem, count))
    return days


def unassigned_summary(root: Path) -> str:
    """One line for the whole heavy-day list; a single day keeps its full line."""
    days = unassigned_days(root)
    if len(days) <= 1:
        return unassigned_nag(root)
    total = sum(count for _, count in days)
    return (
        f"{capture_mod.UNASSIGNED.as_posix()}: {len(days)} heavy days, {total} records with no feature "
        f"(latest {days[-1][0]}) — adopt by mapping a feature with the `feature_use` tool; days listed in the full-list file"
    )


def unassigned_nag(root: Path) -> str:
    """Report unassigned capture days above the heavy-day threshold with their recovery command.
    Initial prompts arrive before intake mapping, so a handful is normal and stays quiet.
    """
    base = root / capture_mod.UNASSIGNED
    if not base.exists():
        return ""
    lines: list[str] = []
    for path in sorted(base.glob("*.jsonl")):
        try:
            count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            continue
        if count > UNASSIGNED_HEAVY_RECORDS:
            lines.append(
                f"{capture_mod.UNASSIGNED.as_posix()}/{path.stem}: {count} records with no feature — "
                "the session that produced them adopts them by mapping a feature with the `feature_use` tool"
            )
    return "\n".join(lines)


MISSING_SHOWN = 7


def journal_nag(root: Path) -> str:
    """Days since 2026-09-03 with activity and no journal block yet, plus the
    milestones that have landed today and not been journaled.

    The backlog and milestones remain available at session start. No git subprocess here: `missing_days`
    reads only features, dispatch records, ADRs and the note itself, and the pending list is
    one small file. The pending line instructs: write today's journal."""
    from .. import journal as journal_mod
    from ..core import today
    from ..journal import pending as pending_mod

    if not journal_mod.is_writer(root):
        return ""
    lines: list[str] = []
    try:
        days = journal_mod.missing_days(root)
    except Exception:  # noqa: BLE001
        days = []
    if days:
        # Limit the routine nudge to one week to keep context bounded; /journal missing lists the
        # full backlog.
        recent, older = days[-MISSING_SHOWN:], len(days) - len(days[-MISSING_SHOWN:])
        more = f" and {older} older" if older else ""
        lines.append(
            f"journal missing for: {', '.join(d.isoformat() for d in recent)}{more} — run /journal missing"
        )
    try:
        pending_line = pending_mod.summary(root, today())
    except Exception:  # noqa: BLE001
        pending_line = ""
    if pending_line:
        lines.append(pending_line)
    return "\n".join(lines)
