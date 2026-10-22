"""The byte budget for `session-start`'s `additionalContext`.

The client moves a hook output past about 10,000 characters to a file and shows the model a 2 KB
preview, so the block stays under `BUDGET_BYTES` and the full lists go to one file per session.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import features
from ..core import agent_display, agent_path
from ..records.narrow import as_dict_or_none, as_list

BUDGET_BYTES = 9000
DIGEST_BYTES = 2500
DIGEST_ROW_CHARS = 110

# Sections that move to the file first when the block is still over budget: lowest priority first.
SPILL_ORDER = ("unassigned", "journal", "lessons", "notes", "dirty", "digest", "others", "pending")


def size(text: str) -> int:
    return len(text.encode("utf-8"))


def clip(text: str, limit: int) -> str:
    """Whole lines of `text` up to `limit` bytes; a note counts what was left out."""
    if size(text) <= limit:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = size(line) + 1
        if used + cost > limit - 60 and kept:
            break
        if used + cost > limit - 60:
            line = line.encode("utf-8")[: max(limit - 80, 0)].decode("utf-8", "ignore") + "…"
        kept.append(line)
        used += size(line) + 1
    left = len(lines) - len(kept)
    if left > 0:
        kept.append(f"… {left} more line(s) in the full-list file")
    return "\n".join(kept)


def full_list_path(root: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "no-session"
    return agent_path(root, "scratch", "session-start", f"{safe}.md")


def full_list_display(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "no-session"
    return agent_display("scratch", "session-start", f"{safe}.md")


def write_full_lists(root: Path, session_id: str, sections: list[tuple[str, str]]) -> bool:
    """One markdown file of every section in full. False when it could not be written."""
    body = "# session-start, full lists\n\n" + "\n\n".join(
        f"## {name}\n\n{text.strip()}" for name, text in sections if text.strip()
    )
    path = full_list_path(root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _status_cell(f: dict[str, Any]) -> str:
    """Keep the blocked reason and flag work that may make the status stale."""
    parked = " (parked)" if f.get("parked") else ""
    if f["status"] != "blocked":
        return str(f["status"]) + parked
    since = as_dict_or_none(f.get("blocked_since"))
    work = [str(w) for w in as_list(f.get("blocked_work"))]
    text = ("blocked? ⚠ active work: " + "; ".join(work) if work else "blocked") + parked
    if since:
        text += f" (blocked since {since.get('ts')}: {since.get('reason')})"
    blocker = as_dict_or_none(f.get("blocker"))
    if blocker:
        text += f" · next: {blocker.get('waits_on')} · resume: {blocker.get('resume_when')}"
    return text


def _landed_mark(indexes: object) -> str:
    landed = [t for t in as_list(indexes) if isinstance(t, int)]
    return f"#{', #'.join(str(t) for t in landed)} landed, unticked; " if landed else ""


def status_digest(data: dict[str, Any], limit: int | None = DIGEST_BYTES) -> str:
    """Open features from `status.collect`, without a generated status file.

    Keep the full rows for the spill file; the session block retains the old row and byte caps.
    """
    order = {"active": 0, "blocked": 1, "proposed": 2}
    opened = sorted(
        (f for f in data["features"] if f["status"] in features.OPEN_STATUSES),
        key=lambda f: (order.get(f["status"], 9), f["id"]),
    )
    lines = [f"## Open features ({len(opened)})"]
    for f in opened:
        state = _status_cell(f)
        next_task = f["next_task"] or "—"
        if f.get("parked"):
            next_task = "— (parked: see the plan's restart note)"
        else:
            if f["status"] == "active" and not f["open_tasks"] and f["done_tasks"]:
                next_task = "active, 0 open: ready to close"
            next_task = _landed_mark(f.get("landed_unticked")) + next_task
        row = f"{f['id']:03d} {f['title']} [{state}] next: {next_task}".replace("\n", " ")
        if limit is not None and len(row) > DIGEST_ROW_CHARS:
            row = row[: DIGEST_ROW_CHARS - 1] + "…"
        lines.append("  " + row)
    if not opened:
        lines.append("_nothing in flight_")
    text = "\n".join(lines)
    text = clip(text, limit) if limit is not None else text
    requests = request_digest(data, REQUEST_DIGEST_BYTES if limit is not None else None)
    return f"{text}\n{requests}" if requests else text


REQUEST_DIGEST_BYTES = 800


def request_digest(data: dict[str, Any], limit: int | None = REQUEST_DIGEST_BYTES) -> str:
    """Active requests and proposed captures, one compact row each: identity, status, category,
    ask and next action. Resolved requests stay out; `request` action `list` has them."""
    order = {"active": 0, "blocked": 1, "proposed": 2}
    rows = [r for r in (as_dict_or_none(item) for item in as_list(data.get("requests"))) if r is not None]
    shown = sorted(
        (r for r in rows if r.get("status") in features.OPEN_STATUSES),
        key=lambda r: (order.get(str(r["status"]), 9), str(r.get("id")).zfill(6)),
    )
    if not shown:
        return ""
    lines = [f"## Requests ({len(shown)})"]
    for r in shown:
        row = f"Request {r['id']:03d} [{r['status']}] {r.get('category') or ''}: {r.get('ask') or '—'} next: {r.get('next_action')}"
        row = row.replace("\n", " ")
        if limit is not None and len(row) > DIGEST_ROW_CHARS + 40:
            row = row[: DIGEST_ROW_CHARS + 39] + "…"
        lines.append("  " + row)
    text = "\n".join(lines)
    return clip(text, limit) if limit is not None else text


def fit(
    parts: dict[str, str], assemble: Callable[[dict[str, str], str], str], root_note: str
) -> tuple[str, list[str]]:
    """Assemble, then spill the lowest-priority sections to the file until under budget.

    `assemble(parts, moved_note)` builds the block. Returns it and the names that moved."""
    moved: list[str] = []
    text = assemble(parts, "")
    for name in SPILL_ORDER:
        if size(text) <= BUDGET_BYTES:
            break
        if not parts.get(name):
            continue
        parts = {**parts, name: ""}
        moved.append(name)
        text = assemble(parts, f"moved to {root_note} to stay under {BUDGET_BYTES} bytes: {', '.join(moved)}")
    return text, moved
