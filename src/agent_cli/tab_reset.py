"""The boundary between a Codex thread's resume and a reset of its tab.

An ended Codex session's tab is reset to the plain terminal title through the shared drainer.
`codex resume` keeps the thread id and so the tab's display, so a reset must not stay on a tab
once the thread has resumed. The drainer selects entries under its lock and paints after
releasing it, so a reset is checked and painted inside this thread's `reset_lock`, and a resume
marks the thread and withdraws its queued reset inside the same lock.

What holds, and what does not. When the resume gets the lock, it comes wholly before or after a
reset paint: before, and the reset is refused; after, and the tab is blank only until the
resumed thread repaints it. The resume runs in a hook with a 5 s timeout, so its wait is bounded
(`RESUME_BUDGET_S`, for both locks together). When it cannot get the lock in time it marks the
thread without it, and a paint already past its check may clear the resumed tab. The drainer
reads the record again after every reset paint, still inside the lock, and if the thread has
resumed it restores the tab immediately after, with the label the thread's current state calls
for. The owner accepted that brief blank.

The reset's response can be lost (a timeout, a failed exit, a garbled reply) after Obsidian ran
it. So each reset tags the leaf it clears with its own random marker, in the same synchronous
step. A lost response is settled by that marker: a marked leaf shows the reset ran, and with
none the reset is cancelled in the page, so it cannot run after a later resume. If the page
cannot confirm either, that is recorded under `UNSETTLED_FIELD`.

Each eval opens a window in which a resume can be marked, so no eval decides alone. Every way
out of `apply` ends in one reconcile step, after the last eval and still inside the lock. It
reads the record once. Resumed: the leaf carrying this reset's marker is filled from the thread's
current state if still blank, and the marker removed; a restore that does not resolve is
recorded under `RESTORE_FAILED_FIELD`, and the marker stays. Still ended: the reset stands and the
marker is removed. That read is the line: a resume marked before it is restored, one marked after
it finds a tab its thread's own events repaint. Only the reconcile step removes a marker.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import core, sessions, tab_drain
from .records.narrow import dig

RESET_EVAL_TIMEOUT_S = 2.0
RESET_EVAL_LOCK_WAIT_S = 5.0
"""The longest a reset eval waits for `tab_drain.EVAL_LOCK`, held by a claim or a `dispatch
open` eval (the drainer's own batches run in this process, never beside it)."""
RESET_LOCK_WAIT_S = 2.5
"""The longest a resume waits for the reset lock: one reset paint, whose own timeout is shorter."""
RESUME_BUDGET_S = 3.0
"""The whole of a resume's waiting, on the reset lock and then the drainer lock together, well
inside the 5 s hook timeout."""
RESTORE_FAILED_FIELD = "tab_restore_failed"
_RESET_RE = re.compile(r"AGENT_TAB_RESET:(done|none|ambiguous|cancelled)\b")
_RESTORE_RE = re.compile(r"AGENT_TAB_RESTORE:(done|kept|unmarked|gone)\b")
_SETTLE_RE = re.compile(r"AGENT_TAB_SETTLE:(marked|cancelled)\b")
UNSETTLED_FIELD = "tab_reset_unsettled"
"""A reset whose outcome was lost and whose cancellation the page could not confirm: it may still
clear the tab later, after a resume. Kept so that change is never silent."""


@contextlib.contextmanager
def exclusive(root: Path, name: str, deadline: float) -> Generator[None]:
    """Hold `.agent/tabs/<name>` exclusively, waiting no later than `deadline` (monotonic).
    Raises TimeoutError. The same file and mechanism as `tab_drain._locked` for its lock, and
    what `tab_drain.eval_lock` holds around every Obsidian eval."""
    if sys.platform == "win32":
        from . import win_lock

        fd = win_lock.open_lock_file(root, (*tab_drain.TABS_DIR.parts, name))
        try:
            with win_lock.exclusive(fd, timeout=max(0.0, deadline - time.monotonic())):
                yield
        finally:
            os.close(fd)
        return
    base = tab_drain.drain_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    fcntl = tab_drain.fcntl
    with (base / name).open("a") as fh:
        if fcntl is not None:
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{name} is busy") from None
                    time.sleep(0.02)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def reset_lock(
    root: Path, identity: str, deadline: float | None = None
) -> contextlib.AbstractContextManager[None]:
    """This thread's reset lock, waited on until `deadline` or for `RESET_LOCK_WAIT_S`."""
    until = time.monotonic() + RESET_LOCK_WAIT_S if deadline is None else deadline
    return exclusive(root, f"reset-{tab_drain.drain_key('manager', identity)}.lock", until)


def cancel_reset(root: Path, identity: str, deadline: float | None = None) -> bool:
    """Withdraw a queued reset for this manager, waiting on the drainer lock no later than
    `deadline`. True when one was withdrawn. On a timeout the request stays queued, and the
    drainer refuses it for a resumed thread. Never raises."""
    path = tab_drain.drain_dir(root) / f"{tab_drain.drain_key('manager', identity)}.json"
    if not path.exists():
        return False
    until = time.monotonic() + RESUME_BUDGET_S if deadline is None else deadline
    try:
        with exclusive(root, "drainer.lock", until):
            if not tab_drain.read_entry(path).get("reset"):
                return False
            path.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001 — a busy lock leaves the request for the drainer to refuse
        return False


def mark_resumed(root: Path, session_id: str, record: dict[str, Any]) -> None:
    """Mark the thread resumed and withdraw its queued reset, inside its reset lock when that
    can be had within the budget, and without it otherwise. Waits at most `RESUME_BUDGET_S` in
    total. Never raises."""
    deadline = time.monotonic() + RESUME_BUDGET_S

    def mark() -> None:
        if not record.get(tab_drain.RESUMED_FIELD):
            sessions.set_field(root, session_id, tab_drain.RESUMED_FIELD, True)
        cancel_reset(root, session_id, deadline)

    try:
        with reset_lock(root, session_id, min(deadline, time.monotonic() + RESET_LOCK_WAIT_S)):
            mark()
            return
    except Exception:  # noqa: BLE001, S110 — marked below without the lock
        pass
    with contextlib.suppress(Exception):
        mark()


def wanted(root: Path, entry: dict[str, Any]) -> bool:
    """Whether a selected reset still stands: still queued as selected, and its thread still
    ended and never resumed. Read under `reset_lock`."""
    queued = tab_drain.read_entry(tab_drain.drain_dir(root) / f"{entry['key']}.json")
    if not queued.get("reset") or queued.get("label") != entry.get("label"):
        return False
    record = sessions.get(root, str(entry.get("identity") or ""))
    return bool(
        record and not record.get(tab_drain.RESUMED_FIELD) and dig(record, "activity", "state") == "ended"
    )


_CANCELLED = "(globalThis.__agentResetCancelled = globalThis.__agentResetCancelled || {})"


def reset_code(display: str, marker: str) -> str:
    """JS that clears `userTitle` on the one tab carrying `display` exactly. In the same
    synchronous step it tags that leaf with `marker`, this reset's own random token, so the tab
    can be found again when the eval's response is lost. A reset the restore already cancelled
    does nothing. Zero or several matches change nothing."""
    return (
        "(() => { const display = " + json.dumps(display) + "; const marker = " + json.dumps(marker) + "; "
        f"if ({_CANCELLED}[marker]) return 'AGENT_TAB_RESET:cancelled'; const hits = []; "
        "for (const leaf of app.workspace.getLeavesOfType('terminal:terminal')) { "
        "const s = leaf.view && leaf.view.state; if (!s) continue; const user = String(s.userTitle || ''); "
        "if (display && (user === display || user.endsWith(' ' + display))) hits.push(leaf); } "
        "if (hits.length !== 1) return 'AGENT_TAB_RESET:' + (hits.length ? 'ambiguous' : 'none'); "
        "hits[0].agentResetToken = marker; "
        "const v = hits[0].view; const c = structuredClone(v.state); c.userTitle = ''; v.state = c; "
        "return 'AGENT_TAB_RESET:done'; })()"
    )


def _find(marker: str) -> str:
    return (
        "const marker = " + json.dumps(marker) + "; "
        "const leaf = app.workspace.getLeavesOfType('terminal:terminal')"
        ".find((l) => l.agentResetToken === marker); "
    )


def restore_code(marker: str, label: str) -> str:
    """Reconcile JS for a resumed thread: put `label` on the leaf carrying this reset's `marker`
    only while it is still blank, then remove the marker. No leaf with the marker means the reset
    cleared nothing (it tags and clears in one step); it is then cancelled in the page, so a late
    run of it does nothing either."""
    return (
        "(() => { " + _find(marker) + f"if (!leaf) {{ {_CANCELLED}[marker] = true; "
        "return 'AGENT_TAB_RESTORE:unmarked'; } "
        "const v = leaf.view; const s = v && v.state; let result = 'AGENT_TAB_RESTORE:gone'; "
        "if (s && String(s.userTitle || '') !== '') result = 'AGENT_TAB_RESTORE:kept'; "
        "else if (s) { const c = structuredClone(s); c.userTitle = " + json.dumps(label) + "; "
        "v.state = c; result = 'AGENT_TAB_RESTORE:done'; } "
        "delete leaf.agentResetToken; return result; })()"
    )


def unmark_code(marker: str) -> str:
    """Reconcile JS for a thread still ended: the reset stands, so only its marker is removed."""
    return (
        "(() => { " + _find(marker) + "if (!leaf) return 'AGENT_TAB_UNMARK:unmarked'; "
        "delete leaf.agentResetToken; return 'AGENT_TAB_UNMARK:done'; })()"
    )


def settle_code(marker: str) -> str:
    """JS that settles a reset whose response was lost: a leaf carrying the marker shows the reset
    ran, and the marker stays for the reconcile step; with none the reset is cancelled in the
    page, so it can never run later. It never removes the marker."""
    return (
        "(() => { " + _find(marker) + "if (leaf) return 'AGENT_TAB_SETTLE:marked'; "
        f"{_CANCELLED}[marker] = true; return 'AGENT_TAB_SETTLE:cancelled'; }})()"
    )


def _settle(root: Path, session_id: str, marker: str) -> str | None:
    """`marked`, `cancelled`, or None when the page could not confirm either. None is recorded
    under `UNSETTLED_FIELD`: the reset may still run later, even after a resume. Never raises."""
    out = None
    with contextlib.suppress(Exception):
        out = _run_eval(settle_code(marker), RESET_EVAL_TIMEOUT_S)
    found = _SETTLE_RE.search(out or "")
    if found:
        return found.group(1)
    reason = "obsidian eval failed or timed out" if out is None else f"unclear reply: {out.strip()[:80]}"
    with contextlib.suppress(Exception):
        record = {"at": core.iso(core.now()), "marker": marker, "reason": reason}
        sessions.set_field(root, session_id, UNSETTLED_FIELD, record)
    return None


def _run_eval(code: str, timeout: float) -> str | None:
    """`obsidian eval` output, or None when it could not run, failed, or timed out. Runs under
    the drainer's `EVAL_LOCK` (resets are applied by the drainer, `tab_drain.drain_root`), and
    how it ended is a `metrics.jsonl` row."""
    return tab_drain.run_eval(
        tab_drain.drain_root, code, timeout, source="reset", wait=RESET_EVAL_LOCK_WAIT_S
    ).stdout


def _restore(root: Path, session_id: str, marker: str, *, cleared: bool) -> None:
    """Put the resumed thread's label back on the tab carrying this reset's marker, from the
    thread's current state. `cleared` is whether the reset reported clearing a tab; when its
    response was lost it is False, and finding no marked tab then means it cleared nothing.

    Only `done` (filled), `kept` (repainted meanwhile, left alone) and an uncleared `unmarked`
    resolve it. Anything else is recorded on the session record under `RESTORE_FAILED_FIELD`,
    and only `done` or `kept` clears an earlier record. Nothing raises."""
    from .hooks.activity import desired_label

    label, reason, resolved = "", "", False
    try:
        desired = desired_label(root, session_id, sessions.get(root, session_id) or {})
        if desired is None:
            reason = "the session has no name to label the tab with"
        else:
            label = desired[2]
            out = _run_eval(restore_code(marker, label), RESET_EVAL_TIMEOUT_S)
            result = _RESTORE_RE.search(out or "")
            outcome = result.group(1) if result else None
            if out is None:
                reason = "obsidian eval failed or timed out"
            elif outcome in ("done", "kept"):
                resolved = True
            elif outcome == "unmarked" and not cleared:
                return  # the reset cleared nothing, and is now cancelled in the page
            elif outcome == "unmarked":
                reason = "the cleared tab no longer carries its reset marker"
            else:
                reason = f"tab not restored: {(out or '').strip()[:80]}"
    except Exception as exc:  # noqa: BLE001 — recorded below
        reason = f"restore raised {type(exc).__name__}"
    with contextlib.suppress(Exception):
        if reason:
            failure = {"at": core.iso(core.now()), "label": label, "reason": reason}
            sessions.set_field(root, session_id, RESTORE_FAILED_FIELD, failure)
        elif resolved and (sessions.get(root, session_id) or {}).get(RESTORE_FAILED_FIELD):
            sessions.set_field(root, session_id, RESTORE_FAILED_FIELD, None)


@dataclass
class Attempt:
    """One reset's progress through `apply`. `outcome` is "unrun" until the reset eval starts,
    then "unknown" until its reply proves `done`, `none`, `ambiguous` or `cancelled`. `settle` is
    the settle eval's finding for an unknown outcome: `marked`, `cancelled`, or None."""

    key: str
    session_id: str
    marker: str
    outcome: str = "unrun"
    settle: str | None = None
    result: set[str] | None = None


def _attempt(root: Path, entry: dict[str, Any], attempt: Attempt) -> None:
    """The reset eval, and the settle eval when its outcome is unknown. Neither removes the
    marker; that is the reconcile step's, after it has acted."""
    if not wanted(root, entry):
        return
    attempt.outcome = "unknown"
    out = None
    with contextlib.suppress(Exception):
        out = _run_eval(reset_code(str(entry.get("match") or ""), attempt.marker), RESET_EVAL_TIMEOUT_S)
    found = _RESET_RE.search(out or "")
    if found:
        attempt.outcome = found.group(1)
    else:
        attempt.settle = _settle(root, attempt.session_id, attempt.marker)


def _reconcile(root: Path, attempt: Attempt) -> set[str]:
    """The one way out of `apply`: after the last eval, read the record and act on it. A
    resumed thread whose tab carries this reset's marker gets it restored from its current
    state; a thread still ended keeps the reset, and its marker is removed. Runs once; never
    raises. Returns the keys painted, for the drainer."""
    if attempt.result is not None:
        return attempt.result
    result: set[str] = set()
    try:
        if attempt.outcome == "ambiguous":
            result = {f"ambiguous.{attempt.key}"}
        elif attempt.outcome in ("done", "unknown") and attempt.settle != "cancelled":
            marked = attempt.outcome == "done" or attempt.settle == "marked"
            record = sessions.get(root, attempt.session_id) or {}
            ended = dig(record, "activity", "state") == "ended"
            if record.get(tab_drain.RESUMED_FIELD) or not ended:
                _restore(root, attempt.session_id, attempt.marker, cleared=marked)
            elif marked:
                with contextlib.suppress(Exception):
                    _run_eval(unmark_code(attempt.marker), RESET_EVAL_TIMEOUT_S)
                result = {attempt.key}
            # unknown and unconfirmed: `_settle` recorded it, and nothing counts as painted
    except Exception:  # noqa: BLE001, S110 — the drainer must keep painting the other tabs
        result = set()
    attempt.result = result
    return result


def apply(root: Path, entry: dict[str, Any]) -> set[str]:
    """Check one selected reset again, paint it, and reconcile, all inside the thread's reset
    lock. Every way out passes through `_reconcile`, which reads the record after the last eval:
    a resume marked at any point before that read gets its tab restored immediately after, from
    its current state. A lost response (timeout, failed exit, garbled reply) does not prove the
    title was left alone, so it is settled by the reset's marker first. A busy lock runs nothing,
    and the reconcile then has nothing to act on."""
    attempt = Attempt(str(entry["key"]), str(entry.get("identity") or ""), uuid.uuid4().hex)
    try:
        with reset_lock(root, attempt.session_id):
            with contextlib.suppress(Exception):
                _attempt(root, entry, attempt)
            return _reconcile(root, attempt)
    except Exception:  # noqa: BLE001 — a busy or broken lock; reconcile still has the last word
        return _reconcile(root, attempt)
