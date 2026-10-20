"""`notes-sync` (see ADR-0034): the hooks around `agent_cli.notes_sync`.

- Session start pulls inside `session-start` (`startup_pull`, then `mark_pull` once the
  session's pid is known), and re-renders vault views after a pull that brought commits, so the
  block it builds shows the pulled records.
- Stop commits and publishes, and re-renders when its rebase brought upstream commits in
  (`Result.incoming`). Every outcome is kept for doctor and told to telemetry by the core
  (`notes_sync.note_outcome`); an error this module would swallow is kept and told the same way.
- UserPromptSubmit pulls once per client process: a resumed
  session in a new process pulls again, a failed pull retries after `RETRY_S`, and a session
  whose process cannot be proven pulls unless it pulled successfully in the last
  `UNPROVEN_FRESH_S` (every prompt before). A prompt that names a notes commit
  (`notes-commit: <sha>`, how a machine hands over evidence it has pushed) pulls whenever that
  commit is not here yet, whatever the freshness.
- A result the core did not write to its state, from Stop or from a prompt's pull, is kept on
  the session record until a sync verifies local and upstream equal, and the next prompt reports
  it. Codex cannot add context at Stop. A disabled or unlinked notes repo clears nothing.
- After any pull or sync that ran, every hard-linked private file git severed is relinked when
  its copy is stale, or refused when it holds an edit, and each file's outcome is kept and
  reported the same way. An outcome stays until a prompt or startup reports it, and a refusal
  or missing file until a later check finds the file linked; git convergence clears neither.
- With the switch off, no hook runs a git command: reporting then reads the session record only.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .. import config_file, notes_sync, sessions
from ..records.narrow import as_dict, as_dict_or_none, as_float, as_list, dict_list

PULL_KEY = "notes_sync_pull"
SHOWN_KEY = "notes_sync_shown"
PENDING_KEY = "notes_sync_pending"
RELINK_KEY = "notes_sync_relink"
RETRY_S = 300.0
QUIET = ("off", "none")
# Outcomes that are neither a failure nor proof of recovery. `locked` means another session is
# syncing this repo right now; the next trigger runs again.
BENIGN = ("off", "none", "current", "pulled", "ahead", "pushed", "locked")
PULL_BENIGN = (*BENIGN, "deferred")  # a pull waits for Stop when the tree is dirty
PULL_OK = ("current", "pulled", "ahead", "deferred")


def _line(r: notes_sync.Result) -> str:
    if r.status == "pulled":
        what = f"pulled {r.count} commit{'s' if r.count != 1 else ''}"
        return f"notes repo: {what}{f'; {r.detail}' if r.detail else ''}."
    if r.status == "current":
        return "notes repo: current."
    return f"notes repo: {r.status}: {r.detail}" if r.detail else f"notes repo: {r.status}."


def relink_diverged(root: Path) -> list[dict[str, str]]:
    """Repair Windows hard links severed by git replacement of a notes file. Relink only entries this
    clone linked; park committed stale copies under `.agent/scratch/notes-diverged/` without
    deleting them. Refuse edited copies and preserve both sides. Report per-file outcomes
    independently so one refusal does not stop the rest.
    """
    import os

    from .. import notes

    notes_root = notes.read_notes_root(root)
    if notes_root is None:
        return []
    record = notes.linked_record(root)
    out: list[dict[str, str]] = []
    for entry in notes.entries(notes_root):
        path, target = root / entry.path, Path(os.path.normpath(notes_root / entry.path))
        if entry.kind != "file" or record.get(entry.path) != str(target):
            continue
        try:
            status, detail = _relink_one(root, notes_root, entry.path, path, target)
        except Exception as exc:  # noqa: BLE001 — reported per file, the rest still run
            status = "relink refused"
            detail = (
                f"{entry.path} no longer shares the notes copy and could not be relinked ({exc}). "
                f"Both copies are kept: this clone's at {path}, the notes copy at {target}"
            )
        out.append({"path": entry.path, "status": status, "detail": detail})
    return out


def _relink_one(root: Path, notes_root: Path, rel: str, path: Path, target: Path) -> tuple[str, str]:
    from .. import links, notes

    if not target.is_file():
        return (
            "link missing",
            f"{rel}: the notes copy {target} is missing; restore it, then run `agent notes link`",
        )
    if links.points_at(path, target) and path.is_file():
        return "linked", ""
    if not path.exists() and not path.is_symlink():
        return "link missing", f"{rel}: this clone's copy {path} is missing; run `agent notes link`"
    if links.is_link(path) or not path.is_file():
        return "relink refused", f"{rel}: {path} is not a plain copy of {target}; both are left as they are"
    data = path.read_bytes()
    if not notes.is_committed_copy(notes_root, rel, data):
        return "relink refused", f"{rel}: {notes.diverged_edit(path, target)}"
    parked = links.recover(root, path, target, expected=data)
    return "relinked", f"{rel} relinked to {target}; this clone's previous copy is at {parked}"


def _after(root: Path, r: notes_sync.Result) -> None:
    if r.status not in QUIET:
        try:
            r.links = relink_diverged(root)
        except Exception as exc:  # noqa: BLE001 — a hook never raises; the owner still hears it
            detail = f"could not check the notes links ({type(exc).__name__}: {exc}); use the `harness` tool's `doctor` action"
            r.links = [{"path": "", "status": "relink refused", "detail": detail}]


def startup_pull(root: Path) -> notes_sync.Result:
    r = notes_sync.pull(root)
    _after(root, r)
    refresh_views(root, r, "pull")
    return r


def refresh_views(root: Path, r: notes_sync.Result, source: str) -> None:
    """Re-render after upstream notes commits arrive: local vault views and `.agent/status.json` need
    rebuilding even after Stop publishes a rebase. Record render failure through
    status.render_quietly without raising.
    """
    if not r.incoming:
        return
    from .. import status

    status.render_quietly(root, source=f"notes-sync {source}")


def _hook_failed(root: Path, payload: dict[str, Any], event: object, exc: Exception) -> None:
    """Fail the hook open while logging the exception and retaining it for doctor."""
    from . import decision_log

    decision_log.error(root, "notes-sync", payload, exc, 0.0)
    notes_sync.note_outcome(root, f"hook {event or 'unknown'}", "error", f"{type(exc).__name__}: {exc}")


def mark_pull(root: Path, session_id: str, pid: int | None, r: notes_sync.Result) -> None:
    """Record this process's pull. Never for `off` or `none`: a later switch-on must still pull."""
    if not session_id or r.status in QUIET:
        return
    try:
        sessions.set_field(
            root, session_id, PULL_KEY, {"pid": pid, "ts": time.time(), "ok": r.status in PULL_OK}
        )
    except Exception:  # noqa: BLE001, S110 — a missed mark only means one more pull
        pass


#: Freshness contract for a session whose client pid is unknown (a Codex manager under the shared
#: daemon): its prompt skips the pull when this session pulled successfully this recently. It used
#: to pull on every prompt, a fetch per machine wake. A handoff that names a notes
#: commit forces the pull regardless (`missing_commits`), so evidence is never read stale.
UNPROVEN_FRESH_S = 120


NOTES_COMMIT_RE = re.compile(r"\bnotes-commit:\s*`?([0-9a-f]{7,64})\b")


def missing_commits(root: Path, text: str) -> list[str]:
    """The notes commits `text` names that this clone's checkout does not contain yet. Evidence
    from another machine arrives as a path plus the notes commit that published it. Present means
    an ancestor of the checked-out HEAD, not merely fetched: a
    fetched object leaves the file absent."""
    from .. import notes

    shas = list(dict.fromkeys(NOTES_COMMIT_RE.findall(text)))
    notes_root = notes.read_notes_root(root) if shas else None
    if notes_root is None:
        return []
    missing: list[str] = []
    for sha in shas:
        try:
            found = subprocess.run(
                ["git", "-C", str(notes_root), "merge-base", "--is-ancestor", sha, "HEAD"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except OSError, subprocess.SubprocessError:
            missing.append(sha)  # unknown counts as missing: the warning is the safe answer
            continue
        if found.returncode != 0:
            missing.append(sha)
    return missing


def _still_missing(root: Path, text: str) -> list[str]:
    """A line naming each referenced commit that is still absent: not published yet, or local
    changes kept the pull from applying it. The next prompt tries again."""
    still = missing_commits(root, text)
    if not still:
        return []
    return [
        f"notes commit {', '.join(s[:12] for s in still)} named in a message is not in this notes "
        "checkout yet (not published, or local changes deferred the pull). Do not use that evidence "
        "until it is; ask the sender to run `notes_publish` if it says so."
    ]


#: Notes commits a consumed message named that were still missing: kept on the session record so
#: the next prompt pulls for them again after the inbox row is gone.
EVIDENCE_KEY = "notes_evidence_pending"


def remember_missing(root: Path, sid: str, text: str) -> list[str]:
    """Record which commits `text` names that are still missing, replacing the earlier list,
    and return them."""
    still = missing_commits(root, text)
    if sid:
        record = sessions.get(root, sid) or {}
        if still or record.get(EVIDENCE_KEY):
            sessions.set_field(root, sid, EVIDENCE_KEY, still or None)
    return still


def evidence_lines(root: Path, text: str, sid: str = "") -> list[str]:
    """For a hook that has just consumed inbox rows (`text`): pull when they name a notes commit
    this checkout lacks, and report one still missing. The prompt hooks run in an order where the
    inbox is drained before `notes-sync` sees it, so the consumer checks too. Never raises; when the check itself fails, it warns rather than stay
    silent (round 3)."""
    try:
        text = _with_pending(root, sid, text)
        if not missing_commits(root, text):
            return []
        r = startup_pull(root)
        lines = [_line(r)] if r.status == "pulled" else []
        remember_missing(root, sid, text)
        return lines + _still_missing(root, text)
    except Exception as exc:  # noqa: BLE001 — a hook never raises; doctor still sees it
        notes_sync.note_outcome(root, "evidence", "error", f"{type(exc).__name__}: {exc}")
        if NOTES_COMMIT_RE.search(text):
            return [
                "a message names a notes commit, and checking for it failed. Do not use that "
                "evidence until the notes checkout is confirmed to contain it."
            ]
        return []


def _with_pending(root: Path, sid: str, text: str) -> str:
    """`text` plus a reference line for each commit still pending from an earlier message."""
    pending = (sessions.get(root, sid) or {}).get(EVIDENCE_KEY) if sid else None
    extra = [f"notes-commit: {sha}" for sha in as_list(pending) if isinstance(sha, str)]
    return "\n".join([text, *extra])


def _named_text(root: Path, sid: str, prompt: str) -> str:
    """The prompt, the unread inbox and the commits still pending: a peer's evidence reference
    usually arrives as an inbox row on a machine wake, not in the prompt itself."""
    from .. import peers

    try:
        rows = peers.peek(root, sid) if sid else []
    except Exception:  # noqa: BLE001 — an unreadable inbox only means the prompt alone is checked
        rows = []
    return _with_pending(root, sid, "\n".join([prompt, *(str(r.get("text") or "") for r in rows)]))


def _needs_pull(record: dict[str, Any], pid: int | None, now: float) -> bool:
    mark = as_dict_or_none(record.get(PULL_KEY))
    if mark is None:
        return True
    if pid is None:  # an unproven process may be a resume: pull unless one just succeeded
        return not (mark.get("ok") and now - as_float(mark.get("ts")) < UNPROVEN_FRESH_S)
    if mark.get("pid") != pid:
        return True
    return not mark.get("ok") and now - as_float(mark.get("ts")) > RETRY_S


def keep(root: Path, session_id: str, r: notes_sync.Result, benign: tuple[str, ...] = BENIGN) -> None:
    """Keep a result that needs the owner's eyes. Only verified convergence clears it. Relink
    outcomes are kept apart, per file, and only a later relink check replaces them."""
    if not session_id:
        return
    record = sessions.get(root, session_id) or {}
    if r.links is not None:
        shown = {str(i) for i in as_list(record.get(SHOWN_KEY))}
        _keep_links(root, session_id, as_dict(record.get(RELINK_KEY)), r.links, shown)
    old = record.get(PENDING_KEY)
    if r.converged:
        if old:
            sessions.set_field(root, session_id, PENDING_KEY, None)
        return
    if r.status in benign:
        return
    kept = as_dict(old)
    same = kept.get("status") == r.status and kept.get("detail") == r.detail and kept.get("id")
    entry = {"id": kept["id"] if same else uuid.uuid4().hex[:12], "status": r.status, "detail": r.detail}
    sessions.set_field(root, session_id, PENDING_KEY, entry)


def _keep_links(
    root: Path, session_id: str, old: dict[str, Any], outcomes: list[dict[str, str]], shown: set[str]
) -> None:
    """Each file's latest relink outcome, keyed by path. An unchanged outcome keeps its id so it
    is reported once. An earlier outcome stays while it is unreported (under `<path>@<id>` once
    a newer one takes its key), and an unresolved one stays until this check verifies its file
    linked: a file this check could not see is not proof of health. Path "" is a check that
    failed outright; any check that ran resolves it."""
    current = {o["path"]: o for o in outcomes}
    kept: dict[str, Any] = {}
    for path, o in current.items():
        if o["status"] == "linked":
            continue
        prev = as_dict(old.get(path))
        same = prev.get("status") == o["status"] and prev.get("detail") == o["detail"] and prev.get("id")
        kept[path] = {
            "id": prev["id"] if same else uuid.uuid4().hex[:12],
            "status": o["status"],
            "detail": o["detail"],
        }
    ids = {v["id"] for v in kept.values()}
    for key, value in old.items():
        prev = as_dict(value)
        if not prev.get("id") or prev["id"] in ids:
            continue
        path = key.partition("@")[0]
        unreported = str(prev["id"]) not in shown
        unverified = path not in current and path != "" and prev.get("status") != "relinked"
        if unreported or unverified:
            kept[key if key not in kept else f"{path}@{prev['id']}"] = prev
    if kept != old:
        sessions.set_field(root, session_id, RELINK_KEY, kept or None)


def startup_line(root: Path, session_id: str, r: notes_sync.Result) -> str:
    """The session-start block's notes line, plus any failure this session has not seen. What
    was kept is reported before this pull's result may clear it, as on a prompt."""
    earlier = report_lines(root, session_id)
    keep(root, session_id, r, PULL_BENIGN)
    lines = [] if r.status in QUIET else [_line(r)]
    return "\n".join(lines + earlier + report_lines(root, session_id))


def report_lines(root: Path, session_id: str) -> list[str]:
    """Each standing failure or kept result, once per session. With the switch off this reads
    the session record only, so no git command runs."""
    try:
        record = (sessions.get(root, session_id) or {}) if session_id else {}
        shown = [str(i) for i in as_list(record.get(SHOWN_KEY))]
        found: list[Any] = [record.get(PENDING_KEY), *as_dict(record.get(RELINK_KEY)).values()]
        if config_file.notes_sync_mode(root) != "off":
            found.insert(0, notes_sync.standing_failure(root))
        out: list[str] = []
        for item in dict_list(found):
            if item.get("id") and str(item["id"]) not in shown:
                out.append(f"notes sync {item.get('status')}: {item.get('detail')}")
                shown.append(str(item["id"]))
        if out and session_id:
            sessions.set_field(root, session_id, SHOWN_KEY, shown[-20:])
        return out
    except Exception as exc:  # noqa: BLE001 — reporting never fails a hook; doctor still sees it
        notes_sync.note_outcome(root, "report", "error", f"{type(exc).__name__}: {exc}")
        return []


def notes_sync_hook(root: Path, payload: dict[str, Any]) -> str:
    if payload.get("agent_id") or payload.get("agent_type"):
        return ""
    sid = str(payload.get("session_id") or "")
    event = payload.get("hook_event_name")
    try:
        if event == "Stop":
            from ..usage import job as usage_job

            try:
                record = sessions.get(root, sid) if sid else None
                r = notes_sync.sync(root, record)
                _after(root, r)
                refresh_views(root, r, "stop")
                keep(root, sid, r)
            finally:
                usage_job.trigger_quietly(root, "stop")
            return ""
        if event != "UserPromptSubmit":
            return ""
        # What Stop kept is reported before this prompt's pull, which may verify recovery and
        # clear it: the owner still hears that it happened.
        lines = report_lines(root, sid)
        record = (sessions.get(root, sid) or {}) if sid else {}
        pid = sessions.resolve_pid()
        named = _named_text(root, sid, str(payload.get("prompt") or ""))
        wanted = missing_commits(root, named)
        if sid and (wanted or _needs_pull(record, pid, time.time())):
            r = startup_pull(root)
            mark_pull(root, sid, pid, r)
            keep(root, sid, r, PULL_BENIGN)
            if r.status == "pulled":
                lines.append(_line(r))
        if wanted:
            remember_missing(root, sid, named)
            lines += _still_missing(root, named)
        lines += report_lines(root, sid)
    except Exception as exc:  # noqa: BLE001 — a hook never raises; the error is kept, not lost
        _hook_failed(root, payload, event, exc)
        return ""
    if not lines:
        return ""
    context = "\n".join(lines)
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    )
