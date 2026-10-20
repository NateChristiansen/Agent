"""What counts as the receiver's answer on the sending side.

Only the receiver's exact shapes are answers. Anything else is no answer, and no answer never
settles a message as accepted. An ACK means accepted (durably recorded there, queued or about
to be), never read, woken or replied to.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..records.narrow import as_dict_or_none, as_list_or_none
from .config import valid_alias

ACK_LIMIT = 4096
LIST_LIMIT = 64 * 1024
MAX_ROWS = 200
CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

OK_KEYS = {"v", "ok", "state", "queue", "wake", "recovery", "id", "to_name"}
ERR_KEYS = {"v", "ok", "error", "detail"}
STATES = {"accepted", "already-accepted"}
QUEUES = {"queued", "pending", "failed"}
WAKES = {"accepted", "failed", "coalesced", "skipped", "in-progress", "expired"}
RECOVERIES = {"none", "appended", "wake_requested"}
ROW_FIELDS = ("name", "tab", "provider", "feature", "state")


class _Bad(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _Bad("duplicate key")
        out[key] = value
    return out


def _constant(_name: str) -> Any:
    raise _Bad("constant")


def _one_object(out: bytes, limit: int) -> dict[str, Any] | None:
    """Exactly one JSON object line within `limit` bytes, or None."""
    if len(out) > limit:
        return None
    lines = [line for line in out.split(b"\n") if line.strip()]
    if len(lines) != 1:
        return None
    try:
        data = json.loads(
            lines[0].decode("utf-8", errors="strict"), object_pairs_hook=_pairs, parse_constant=_constant
        )
    except ValueError, UnicodeDecodeError, RecursionError:  # RecursionError: deep nesting
        return None
    obj = as_dict_or_none(data)
    if obj is None or type(obj.get("v")) is not int or obj["v"] != 1 or type(obj.get("ok")) is not bool:
        return None
    return obj


def _text(value: Any, limit: int) -> bool:
    """A real string: bounded, no control characters, and valid Unicode (JSON admits lone
    surrogates such as "\\ud800"; UTF-8 does not)."""
    if type(value) is not str or len(value) > limit or CONTROL_RE.search(value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _member(value: Any, allowed: set[str]) -> bool:
    """Membership only for an exact string: a list or dict is unhashable and must never reach
    `in` (a TypeError there crashed the check)."""
    return type(value) is str and value in allowed


def refusal(data: dict[str, Any]) -> str | None:
    """The refusal code of an exact `ok: false` answer, or None if it is not one."""
    if set(data) != ERR_KEYS or data["ok"] is not False:
        return None
    code, detail = data.get("error"), data.get("detail")
    if (
        not isinstance(code, str)
        or not _text(code, 40)
        or not CODE_RE.fullmatch(code)
        or not _text(detail, 64)
    ):
        return None
    return code


def enqueue_answer(status: int | None, out: bytes, msg_id: str) -> dict[str, Any] | None:
    """The receiver's answer to an enqueue of `msg_id`, or None for no answer. The receiver
    exits 0 for ok and 1 for a refusal; any other status, or a mismatch, is no answer."""
    data = _one_object(out, ACK_LIMIT)
    if data is None:
        return None
    if data["ok"] is False:
        return data if status == 1 and refusal(data) else None
    if status != 0 or set(data) != OK_KEYS:
        return None
    if (
        not _member(data["state"], STATES)
        or not _member(data["queue"], QUEUES)
        or not _member(data["wake"], WAKES)
        or not _member(data["recovery"], RECOVERIES)
        or type(data["id"]) is not str
        or data["id"] != msg_id
        or not _text(data["to_name"], 128)
        or not data["to_name"]
    ):
        return None
    return data


def list_answer(status: int | None, out: bytes, alias: str) -> tuple[list[dict[str, Any]], int] | str:
    """(rows, dropped) from the receiver's list answer for `alias`, or a reason code string.
    Each row is rebuilt from the allowlist only; one that fails a check is dropped and counted."""
    data = _one_object(out, LIST_LIMIT)
    if data is None:
        return "no-answer"
    if data["ok"] is False:
        code = refusal(data) if status == 1 else None
        return code or "no-answer"
    if status != 0 or set(data) != {"v", "ok", "device", "sessions"} or data["device"] != alias:
        return "no-answer"
    sessions = as_list_or_none(data["sessions"])
    if sessions is None or not valid_alias(alias):
        return "no-answer"
    rows: list[dict[str, Any]] = []
    dropped = 0
    for item in sessions[:MAX_ROWS]:
        row = _row(item)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return rows, dropped + max(0, len(sessions) - MAX_ROWS)


def _row(value: object) -> dict[str, Any] | None:
    item = as_dict_or_none(value)
    if item is None or set(item) != set(ROW_FIELDS):
        return None
    feature = item["feature"]
    if feature is not None and type(feature) is not int:
        return None
    for key in ("name", "tab", "provider", "state"):
        value = item[key]
        if value is not None and not _text(value, 128):
            return None
    return {key: item[key] for key in ROW_FIELDS}
