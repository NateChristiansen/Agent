# ruff: noqa
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
BENIGN = ("off", "none", "current", "pulled", "ahead", "pushed", "locked")
PULL_BENIGN = (*BENIGN, "deferred")
PULL_OK = ("current", "pulled", "ahead", "deferred")


def _line(r: notes_sync.Result) -> str:
    if r.status == "pulled":
        what = f"pulled {r.count} commit{('s' if r.count != 1 else '')}"
        return f"notes repo: {what}{(f'; {r.detail}' if r.detail else '')}."
    if r.status == "current":
        return "notes repo: current."
    return f"notes repo: {r.status}: {r.detail}" if r.detail else f"notes repo: {r.status}."


def relink_diverged(root: Path) -> list[dict[str, str]]:
    import os
    from .. import notes

    notes_root = notes.read_notes_root(root)
    if notes_root is None:
        return []
    record = notes.linked_record(root)
    out: list[dict[str, str]] = []
    for entry in notes.entries(notes_root):
        path, target = (root / entry.path, Path(os.path.normpath(notes_root / entry.path)))
        if entry.kind != "file" or record.get(entry.path) != str(target):
            continue
        try:
            status, detail = _relink_one(root, notes_root, entry.path, path, target)
        except Exception as exc:
            status = "relink refused"
            detail = f"{entry.path} no longer shares the notes copy and could not be relinked ({exc}). Both copies are kept: this clone's at {path}, the notes copy at {target}"
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
        return ("linked", "")
    if not path.exists() and (not path.is_symlink()):
        return ("link missing", f"{rel}: this clone's copy {path} is missing; run `agent notes link`")
    if links.is_link(path) or not path.is_file():
        return ("relink refused", f"{rel}: {path} is not a plain copy of {target}; both are left as they are")
    data = path.read_bytes()
    if not notes.is_committed_copy(notes_root, rel, data):
        return ("relink refused", f"{rel}: {notes.diverged_edit(path, target)}")
    parked = links.recover(root, path, target, expected=data)
    return ("relinked", f"{rel} relinked to {target}; this clone's previous copy is at {parked}")


def _after(root: Path, r: notes_sync.Result) -> None:
    if r.status not in QUIET:
        try:
            r.links = relink_diverged(root)
        except Exception as exc:
            detail = f"could not check the notes links ({type(exc).__name__}: {exc}); use the `harness` tool's `doctor` action"
            r.links = [{"path": "", "status": "relink refused", "detail": detail}]


def startup_pull(root: Path) -> notes_sync.Result:
    r = notes_sync.pull(root)
    _after(root, r)
    refresh_views(root, r, "pull")
    return r


def refresh_views(root: Path, r: notes_sync.Result, source: str) -> None:
    if not r.incoming:
        return
    from .. import status

    status.render_quietly(root, source=f"notes-sync {source}")


def _hook_failed(root: Path, payload: dict[str, Any], event: object, exc: Exception) -> None:
    from . import decision_log

    decision_log.error(root, "notes-sync", payload, exc, 0.0)
    notes_sync.note_outcome(root, f"hook {event or 'unknown'}", "error", f"{type(exc).__name__}: {exc}")


def mark_pull(root: Path, session_id: str, pid: int | None, r: notes_sync.Result) -> None:
    if not session_id or r.status in QUIET:
        return
    try:
        sessions.set_field(
            root, session_id, PULL_KEY, {"pid": pid, "ts": time.time(), "ok": r.status in PULL_OK}
        )
    except Exception:
        pass


UNPROVEN_FRESH_S = 120
NOTES_COMMIT_RE = re.compile("\\bnotes-commit:\\s*`?([0-9a-f]{7,64})\\b")


def missing_commits(root: Path, text: str) -> list[str]:
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
            missing.append(sha)
            continue
        if found.returncode != 0:
            missing.append(sha)
    return missing


def names_missing_commit(root: Path, text: str) -> bool:
    return bool(missing_commits(root, text))


def _still_missing(root: Path, text: str) -> list[str]:
    still = missing_commits(root, text)
    if not still:
        return []
    return [
        f"notes commit {', '.join((s[:12] for s in still))} named in a message is not in this notes checkout yet (not published, or local changes deferred the pull). Do not use that evidence until it is; ask the sender to run `notes_publish` if it says so."
    ]


EVIDENCE_KEY = "notes_evidence_pending"


def remember_missing(root: Path, sid: str, text: str) -> list[str]:
    still = missing_commits(root, text)
    if sid:
        record = sessions.get(root, sid) or {}
        if still or record.get(EVIDENCE_KEY):
            sessions.set_field(root, sid, EVIDENCE_KEY, still or None)
    return still


def evidence_lines(root: Path, text: str, sid: str = "") -> list[str]:
    try:
        text = _with_pending(root, sid, text)
        if not missing_commits(root, text):
            return []
        r = startup_pull(root)
        lines = [_line(r)] if r.status == "pulled" else []
        remember_missing(root, sid, text)
        return lines + _still_missing(root, text)
    except Exception as exc:
        notes_sync.note_outcome(root, "evidence", "error", f"{type(exc).__name__}: {exc}")
        if NOTES_COMMIT_RE.search(text):
            return [
                "a message names a notes commit, and checking for it failed. Do not use that evidence until the notes checkout is confirmed to contain it."
            ]
        return []


def _with_pending(root: Path, sid: str, text: str) -> str:
    pending = (sessions.get(root, sid) or {}).get(EVIDENCE_KEY) if sid else None
    extra = [f"notes-commit: {sha}" for sha in as_list(pending) if isinstance(sha, str)]
    return "\n".join([text, *extra])


def _named_text(root: Path, sid: str, prompt: str) -> str:
    from .. import peers

    try:
        rows = peers.peek(root, sid) if sid else []
    except Exception:
        rows = []
    return _with_pending(root, sid, "\n".join([prompt, *(str(r.get("text") or "") for r in rows)]))


def _needs_pull(record: dict[str, Any], pid: int | None, now: float) -> bool:
    mark = as_dict_or_none(record.get(PULL_KEY))
    if mark is None:
        return True
    if pid is None:
        return not (mark.get("ok") and now - as_float(mark.get("ts")) < UNPROVEN_FRESH_S)
    if mark.get("pid") != pid:
        return True
    return not mark.get("ok") and now - as_float(mark.get("ts")) > RETRY_S


def keep(root: Path, session_id: str, r: notes_sync.Result, benign: tuple[str, ...] = BENIGN) -> None:
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
        unverified = path not in current and path != "" and (prev.get("status") != "relinked")
        if unreported or unverified:
            kept[key if key not in kept else f"{path}@{prev['id']}"] = prev
    if kept != old:
        sessions.set_field(root, session_id, RELINK_KEY, kept or None)


def startup_line(root: Path, session_id: str, r: notes_sync.Result) -> str:
    earlier = report_lines(root, session_id)
    keep(root, session_id, r, PULL_BENIGN)
    lines = [] if r.status in QUIET else [_line(r)]
    return "\n".join(lines + earlier + report_lines(root, session_id))


def report_lines(root: Path, session_id: str) -> list[str]:
    try:
        record = sessions.get(root, session_id) or {} if session_id else {}
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
    except Exception as exc:
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
        lines = report_lines(root, sid)
        record = sessions.get(root, sid) or {} if sid else {}
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
    except Exception as exc:
        _hook_failed(root, payload, event, exc)
        return ""
    if not lines:
        return ""
    context = "\n".join(lines)
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    )
