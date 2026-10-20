"""Telemetry event schema, version 1 (see ADR-0038).

`validate` returns None for an event the sink may write, or a short reason for a refusal. The
recorder counts every refusal as a loss of kind `refused`; nothing here raises.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from ..records.narrow import as_dict_or_none, as_list_or_none

SCHEMA_VERSION = 1

KINDS = frozenset(
    {
        "dispatch.launch",
        "dispatch.setup",
        "dispatch.preflight",
        "dispatch.verification",
        "dispatch.terminal",
        "dispatch.reverify",
        "dispatch.ack",
        "dispatch.transition",
        "health.probe",
        "hook.denial",
        "hook.error",
        # Attention, permission and peer delivery events.
        "activity.transition",
        "permission.request",
        "peer.enqueue",
        "peer.ring",
        "peer.consume",
        "feature.close",
        "task.add",
        "task.done",
        "journal.write",
        # Notes-sync outcomes and failed view renders.
        "notes.sync",
        "status.render",
        # Landing and certificate operations, including failures.
        "land.op",
        "verify.record",
    }
)
# Workflow events name their own sub-kind after the prefix.
KIND_PREFIXES = ("workflow.",)
SOURCES = frozenset({"live", "backfill"})
COST_SOURCES = frozenset({"provider", "estimate", "unknown"})

# Prompt bodies, outputs and file contents are never recorded; these keys are refused outright.
REFUSED_KEYS = frozenset({"prompt", "text", "body", "output", "message", "content"})
MAX_STRING = 512
MAX_LIST = 32
MAX_LIST_ITEM = 128

FIELDS = (
    "schema",
    "event_id",
    "parent_id",
    "kind",
    "ts",
    "duration_s",
    "outcome",
    "writer",
    "session",
    "feature",
    "dispatch",
    "provider",
    "model",
    "cost_usd",
    "cost_source",
    "commit",
    "source",
    "data",
)
OPTIONAL_STRINGS = ("outcome", "session", "feature", "dispatch", "provider", "model")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def known_kind(kind: object) -> bool:
    return isinstance(kind, str) and (
        kind in KINDS or any(kind.startswith(p) and len(kind) > len(p) for p in KIND_PREFIXES)
    )


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _string(value: object, limit: int = MAX_STRING) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _data(data: object) -> str | None:
    entries = as_dict_or_none(data)
    if entries is None:
        return "data is not an object"
    for key, value in entries.items():
        # _string also refuses a key that is not a str
        if not _string(key):
            return "data key is not a short string"
        if key.lower() in REFUSED_KEYS:
            return f"data key {key!r} is refused"
        items = as_list_or_none(value)
        if items is not None:
            if len(items) > MAX_LIST or not all(_string(item, MAX_LIST_ITEM) for item in items):
                return f"data list {key!r} exceeds its bound"
        elif isinstance(value, str):
            if len(value) > MAX_STRING:
                return f"data string {key!r} exceeds {MAX_STRING} characters"
        elif not (value is None or isinstance(value, bool) or _number(value)):
            return f"data value {key!r} is not a scalar or a list of strings"
    return None


def parse_ts(value: object) -> datetime | None:
    """An ISO-8601 timestamp with an offset, in UTC; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _writer(writer: object) -> str | None:
    fields = as_dict_or_none(writer)
    if fields is None or set(fields) != {"host", "pid", "instance", "seq"}:
        return "writer field set"
    if not _string(fields["host"]) or type(fields["pid"]) is not int or not _uuid(fields["instance"]):
        return "writer host, pid or instance"
    seq = fields["seq"]
    if type(seq) is not int or seq < 1:
        return "writer seq is not a positive integer"
    return None


def validate(event: dict[str, Any]) -> str | None:
    """The refusal reason for `event`, or None when it is a valid version 1 event. The recorder
    refuses with it and health counts every line it rejects as malformed."""
    # Older permission events may omit provider; readers count them as unknown.
    if set(event) | {"provider"} != set(FIELDS):
        return "field set"
    if type(event["schema"]) is not int or event["schema"] != SCHEMA_VERSION:
        return "schema"
    if not known_kind(event["kind"]):
        return "unknown kind"
    if not _uuid(event["event_id"]):
        return "event_id is not a uuid"
    if event["parent_id"] is not None and not _uuid(event["parent_id"]):
        return "parent_id is not a uuid"
    if parse_ts(event["ts"]) is None:
        return "ts is not an ISO-8601 timestamp with an offset"
    if (reason := _writer(event["writer"])) is not None:
        return reason
    for key in OPTIONAL_STRINGS:
        if event.get(key) is not None and not _string(event[key]):
            return f"{key} is not a string of at most {MAX_STRING} characters"
    for key in ("duration_s", "cost_usd"):
        if event[key] is not None and not _number(event[key]):
            return f"{key} is not a finite number"
    if event["cost_source"] is not None and event["cost_source"] not in COST_SOURCES:
        return "unknown cost_source"
    if event["cost_source"] == "unknown" and event["cost_usd"] is not None:
        return "cost_usd must be null when cost_source is unknown"
    if event["commit"] is not None and not (
        isinstance(event["commit"], str) and _SHA.fullmatch(event["commit"])
    ):
        return "commit is not a hex sha"
    if event["source"] not in SOURCES:
        return "unknown source"
    return _data(event["data"])
