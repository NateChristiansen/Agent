"""Read completion attachments that reach no hook and clear the matching background task ids. Keep
unrelated tasks pending so a manager tab reflects work it still awaits.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import sessions
from ..records.narrow import as_dict, as_dict_or_none, as_int, dict_list, dig, str_list

SCAN_FIELD = "transcript_scan"
MAX_SCAN_BYTES = 4 * 1024 * 1024
"""The most transcript one `Stop` reads, so a hook stays well inside its 5 s timeout. A turn
rarely writes a megabyte. A larger backlog is read in order over the following Stops; the mark
never moves past a byte that was not read. Until a Stop reaches a notice its task stays open, so
an idle manager whose last Stop left a backlog keeps ⏳; the mark's `behind` shows it."""

TERMINAL_STATUSES = frozenset({"completed", "failed", "killed"})
"""Statuses that end a task. `completed` and `failed` are observed: this machine's transcripts
held both (checked). `killed` is an unverified assumption for a stopped task, never
seen written; if Claude Code writes another word, that task stays open."""

_TASK_ID_RE = re.compile(r"<task-id>\s*([\w-]+)\s*</task-id>")
_STATUS_RE = re.compile(r"<status>\s*([\w-]*)\s*</status>")


def finished_ids(text: str) -> set[str]:
    """Task ids from the notifications in `text` that report an end. A `Monitor` sends one
    notification per event with no `<status>`, while it keeps running; only the final one has a
    terminal status, so only that one closes the task. Any other or malformed status does not."""
    out: set[str] = set()
    for block in text.split("<task-notification>")[1:]:
        block = block.split("</task-notification>", 1)[0]
        statuses = _STATUS_RE.findall(block)
        if len(statuses) == 1 and statuses[0] in TERMINAL_STATUSES:
            out.update(_TASK_ID_RE.findall(block))
    return out


def delivered_texts(entry: dict[str, Any]) -> list[str]:
    """The text of a transcript entry that delivered something to the session: a queued
    notification, a mid-turn `queued_command`, or a user message's own text. Tool results and
    assistant text are left out, since either can quote a notification it did not deliver."""
    kind = entry.get("type")
    if kind == "queue-operation":
        return [str(entry.get("content") or "")]
    if kind == "attachment":
        attachment = as_dict(entry.get("attachment"))
        if attachment.get("type") != "queued_command":
            return []
        content = attachment.get("prompt")
    elif kind == "user":
        content = dig(entry, "message", "content")
    else:
        return []
    if isinstance(content, str):
        return [content]
    return [str(block.get("text") or "") for block in dict_list(content) if block.get("type") == "text"]


MAX_UNPARSED = 10
CARRY_CHARS = 1024  # longer than any notice, so one split across two reads is still seen whole


def _mark(path: str, offset: int, size: int, unparsed: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """The scan state kept on the session record. `behind` is what is not read yet: a backlog
    waits for later Stops, so an idle manager can keep ⏳ until a Stop reaches its notice.
    `unparsed` lists entries too long to parse, with the ids of any finished notices seen in
    them; those tasks stay open, since nothing shows the entry delivered them."""
    out: dict[str, Any] = {"path": path, "offset": offset, **extra}
    if size > offset:
        out["behind"] = size - offset
    if unparsed:
        out["unparsed"] = unparsed[-MAX_UNPARSED:]
    return out


def scan(path: str, mark: dict[str, Any]) -> tuple[set[str], dict[str, Any] | None]:
    """Finished task ids in up to `MAX_SCAN_BYTES` of complete lines from `mark`, and the new
    mark, or no mark when the file cannot be read. The mark ends at the last byte read, so a
    line still being written, and any backlog past this read, wait for the next scan.

    The scan is not lossless. An entry longer than one read cannot be parsed in bounded work, so
    nothing shows whether it delivered a notice or only quoted one, as a tool result can. Its
    bytes are read in bounded pieces over as many Stops as it takes, and the finished notices
    seen in them are recorded under `unparsed`, unconfirmed; they close nothing."""
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            same = mark.get("path") == path
            start = int(mark.get("offset") or 0) if same else 0
            in_line: dict[str, object] | None = as_dict_or_none(mark.get("in_line")) if same else None
            unparsed = list(mark.get("unparsed") or []) if same else []
            if start > size:  # the file shrank, so it is not the one marked: read it from the start
                start, in_line, unparsed = 0, None, []
            fh.seek(start)
            data = fh.read(min(size - start, MAX_SCAN_BYTES))
    except OSError, ValueError, TypeError:
        return set(), None
    first = 0
    if in_line is None and data.rfind(b"\n") < 0 and len(data) >= MAX_SCAN_BYTES:
        in_line = {"from": start, "ids": [], "carry": ""}
    if in_line is not None:
        newline = data.find(b"\n")
        piece = data if newline < 0 else data[: newline + 1]
        text = str(in_line.get("carry") or "") + piece.decode("utf-8", errors="replace")
        ids = sorted(set(str_list(in_line.get("ids"))) | finished_ids(text))
        if newline < 0:
            state = {"from": in_line.get("from", start), "ids": ids, "carry": text[-CARRY_CHARS:]}
            return set(), _mark(path, start + len(data), size, unparsed, in_line=state)
        first = newline + 1
        unparsed.append({"from": in_line.get("from", start), "to": start + first, "ids": ids})
    end = data.rfind(b"\n") + 1
    if end <= first:
        return set(), _mark(path, start + first, size, unparsed)
    finished: set[str] = set()
    for line in data[first:end].splitlines():
        if b"task-id" not in line:
            continue
        try:
            entry = as_dict_or_none(json.loads(line))
        except ValueError:
            continue
        if entry is not None:
            for text in delivered_texts(entry):
                finished |= finished_ids(text)
    return finished, _mark(path, start + end, size, unparsed)


def stuck_line(record: dict[str, Any]) -> str:
    """One line for the session-start and prompt context when the scan may be holding ⏳, or "".
    Only while a task is open: unread bytes are behind it, or
    an entry too long to parse showed one of its finished notices. Never raises."""
    try:
        pending = set(record.get("background_tasks") or {})
        mark = as_dict(record.get(SCAN_FIELD))
        if not pending:
            return ""
        reasons: list[str] = []
        behind = as_int(mark.get("behind"))
        if behind > 0:
            reasons.append(f"{behind} transcript bytes are not read yet; a later Stop reads them")
        for entry in dict_list(mark.get("unparsed")):
            ids = sorted(pending & set(str_list(entry.get("ids"))))
            if ids:
                reasons.append(
                    f"an entry too long to parse at bytes {entry.get('from')}-{entry.get('to')} "
                    f"shows {', '.join(ids)} finished, unconfirmed, so it stays open"
                )
        if not reasons:
            return ""
        return "Background tasks: the tab's ⏳ may be stuck: " + "; ".join(reasons) + "."
    except Exception:  # noqa: BLE001 — a context line is a courtesy
        return ""


def close_finished(
    root: Path,
    session_id: str,
    record: dict[str, Any],
    payload: dict[str, Any],
    pending: Mapping[str, object],
) -> dict[str, object] | None:
    """On `Stop`, the outstanding tasks left once the transcript's finished notices are read, or
    None if unchanged. The transcript is read on every Stop, with or without outstanding tasks,
    so the mark always sits at the last byte read. What it could not settle stays on the mark,
    `behind` and `unparsed` (see `_mark`). Never raises on a bad file."""
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    previous = as_dict(record.get(SCAN_FIELD))
    finished, mark = scan(path, previous)
    if mark is not None and mark != previous:
        sessions.set_field(root, session_id, SCAN_FIELD, mark)
    remaining = {k: v for k, v in pending.items() if k not in finished}
    return remaining if len(remaining) != len(pending) else None
