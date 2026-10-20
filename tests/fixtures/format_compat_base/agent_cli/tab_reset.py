# ruff: noqa
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
"The longest a reset eval waits for `tab_drain.EVAL_LOCK`, held by a claim or a `dispatch\nopen` eval (the drainer's own batches run in this process, never beside it)."
RESET_LOCK_WAIT_S = 2.5
"The longest a resume waits for the reset lock: one reset paint, whose own timeout is shorter."
RESUME_BUDGET_S = 3.0
"The whole of a resume's waiting, on the reset lock and then the drainer lock together, well\ninside the 5 s hook timeout."
RESTORE_FAILED_FIELD = "tab_restore_failed"
_RESET_RE = re.compile("AGENT_TAB_RESET:(done|none|ambiguous|cancelled)\\b")
_RESTORE_RE = re.compile("AGENT_TAB_RESTORE:(done|kept|unmarked|gone)\\b")
_SETTLE_RE = re.compile("AGENT_TAB_SETTLE:(marked|cancelled)\\b")
UNSETTLED_FIELD = "tab_reset_unsettled"
"A reset whose outcome was lost and whose cancellation the page could not confirm: it may still\nclear the tab later, after a resume. Kept so that change is never silent."


@contextlib.contextmanager
def exclusive(root: Path, name: str, deadline: float) -> Generator[None]:
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
    until = time.monotonic() + RESET_LOCK_WAIT_S if deadline is None else deadline
    return exclusive(root, f"reset-{tab_drain.drain_key('manager', identity)}.lock", until)


def cancel_reset(root: Path, identity: str, deadline: float | None = None) -> bool:
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
    except Exception:
        return False


def mark_resumed(root: Path, session_id: str, record: dict[str, Any]) -> None:
    deadline = time.monotonic() + RESUME_BUDGET_S

    def mark() -> None:
        if not record.get(tab_drain.RESUMED_FIELD):
            sessions.set_field(root, session_id, tab_drain.RESUMED_FIELD, True)
        cancel_reset(root, session_id, deadline)

    try:
        with reset_lock(root, session_id, min(deadline, time.monotonic() + RESET_LOCK_WAIT_S)):
            mark()
            return
    except Exception:
        pass
    with contextlib.suppress(Exception):
        mark()


def wanted(root: Path, entry: dict[str, Any]) -> bool:
    queued = tab_drain.read_entry(tab_drain.drain_dir(root) / f"{entry['key']}.json")
    if not queued.get("reset") or queued.get("label") != entry.get("label"):
        return False
    record = sessions.get(root, str(entry.get("identity") or ""))
    return bool(
        record and (not record.get(tab_drain.RESUMED_FIELD)) and (dig(record, "activity", "state") == "ended")
    )


_CANCELLED = "(globalThis.__agentResetCancelled = globalThis.__agentResetCancelled || {})"


def reset_code(display: str, marker: str) -> str:
    return (
        "(() => { const display = "
        + json.dumps(display)
        + "; const marker = "
        + json.dumps(marker)
        + f"; if ({_CANCELLED}[marker]) return 'AGENT_TAB_RESET:cancelled'; const hits = []; for (const leaf of app.workspace.getLeavesOfType('terminal:terminal')) {{ const s = leaf.view && leaf.view.state; if (!s) continue; const user = String(s.userTitle || ''); if (display && (user === display || user.endsWith(' ' + display))) hits.push(leaf); }} if (hits.length !== 1) return 'AGENT_TAB_RESET:' + (hits.length ? 'ambiguous' : 'none'); hits[0].agentResetToken = marker; const v = hits[0].view; const c = structuredClone(v.state); c.userTitle = ''; v.state = c; return 'AGENT_TAB_RESET:done'; }})()"
    )


def _find(marker: str) -> str:
    return (
        "const marker = "
        + json.dumps(marker)
        + "; const leaf = app.workspace.getLeavesOfType('terminal:terminal').find((l) => l.agentResetToken === marker); "
    )


def restore_code(marker: str, label: str) -> str:
    return (
        "(() => { "
        + _find(marker)
        + f"if (!leaf) {{ {_CANCELLED}[marker] = true; return 'AGENT_TAB_RESTORE:unmarked'; }} const v = leaf.view; const s = v && v.state; let result = 'AGENT_TAB_RESTORE:gone'; if (s && String(s.userTitle || '') !== '') result = 'AGENT_TAB_RESTORE:kept'; else if (s) {{ const c = structuredClone(s); c.userTitle = "
        + json.dumps(label)
        + "; v.state = c; result = 'AGENT_TAB_RESTORE:done'; } delete leaf.agentResetToken; return result; })()"
    )


def unmark_code(marker: str) -> str:
    return (
        "(() => { "
        + _find(marker)
        + "if (!leaf) return 'AGENT_TAB_UNMARK:unmarked'; delete leaf.agentResetToken; return 'AGENT_TAB_UNMARK:done'; })()"
    )


def settle_code(marker: str) -> str:
    return (
        "(() => { "
        + _find(marker)
        + f"if (leaf) return 'AGENT_TAB_SETTLE:marked'; {_CANCELLED}[marker] = true; return 'AGENT_TAB_SETTLE:cancelled'; }})()"
    )


def _settle(root: Path, session_id: str, marker: str) -> str | None:
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
    return tab_drain.run_eval(
        tab_drain.drain_root, code, timeout, source="reset", wait=RESET_EVAL_LOCK_WAIT_S
    ).stdout


def _restore(root: Path, session_id: str, marker: str, *, cleared: bool) -> None:
    from .hooks.activity import desired_label

    label, reason, resolved = ("", "", False)
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
            elif outcome == "unmarked" and (not cleared):
                return
            elif outcome == "unmarked":
                reason = "the cleared tab no longer carries its reset marker"
            else:
                reason = f"tab not restored: {(out or '').strip()[:80]}"
    except Exception as exc:
        reason = f"restore raised {type(exc).__name__}"
    with contextlib.suppress(Exception):
        if reason:
            failure = {"at": core.iso(core.now()), "label": label, "reason": reason}
            sessions.set_field(root, session_id, RESTORE_FAILED_FIELD, failure)
        elif resolved and (sessions.get(root, session_id) or {}).get(RESTORE_FAILED_FIELD):
            sessions.set_field(root, session_id, RESTORE_FAILED_FIELD, None)


@dataclass
class Attempt:
    key: str
    session_id: str
    marker: str
    outcome: str = "unrun"
    settle: str | None = None
    result: set[str] | None = None


def _attempt(root: Path, entry: dict[str, Any], attempt: Attempt) -> None:
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
    except Exception:
        result = set()
    attempt.result = result
    return result


def apply(root: Path, entry: dict[str, Any]) -> set[str]:
    attempt = Attempt(str(entry["key"]), str(entry.get("identity") or ""), uuid.uuid4().hex)
    try:
        with reset_lock(root, attempt.session_id):
            with contextlib.suppress(Exception):
                _attempt(root, entry, attempt)
            return _reconcile(root, attempt)
    except Exception:
        return _reconcile(root, attempt)
