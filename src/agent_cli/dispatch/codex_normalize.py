"""Convert Codex app-server events to the dispatch result contract."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..records.narrow import as_dict_or_none, as_int, as_list, as_list_or_none

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_FRAMES = 50_000
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_THREAD_FRAMES = frozenset({"turn/started", "turn/completed", "item/started", "item/completed"})
_TURN_STATUSES = frozenset({"completed", "interrupted", "failed", "inProgress"})
_MCP_STATUSES = frozenset({"inProgress", "completed", "failed"})


def codex_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make the shared return schema valid for Codex's strict JSON output mode."""
    strict = deepcopy(schema)

    def visit(node: dict[str, Any]) -> None:
        if node.get("type") == "object":
            properties = as_dict_or_none(node.get("properties"))
            if properties is not None:
                required = set(as_list(node.get("required")))
                node["additionalProperties"] = False
                node["required"] = list(properties)
                for name, value in properties.items():
                    child = as_dict_or_none(value)
                    if child is None:
                        continue
                    visit(child)
                    if name not in required and isinstance(child.get("type"), str):
                        child["type"] = [child["type"], "null"]
        items = as_dict_or_none(node.get("items"))
        if items is not None:
            visit(items)

    visit(strict)
    return strict


def final_text(item: dict[str, Any]) -> str | None:
    if item.get("type") != "agentMessage":
        return None
    if item.get("phase") not in (None, "final_answer"):
        return None
    value = item.get("text")
    return value if isinstance(value, str) else None


def structured_return(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        value = as_dict_or_none(json.loads(text))
    except json.JSONDecodeError:
        return None
    return _omit_null_properties(value) if value is not None else None


def _omit_null_properties(value: dict[str, Any]) -> dict[str, Any]:
    """Strict output makes optional keys nullable; the shared return omits them."""
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            continue
        if (mapping := as_dict_or_none(item)) is not None:
            clean[key] = _omit_null_properties(mapping)
        elif (rows := as_list_or_none(item)) is not None:
            clean[key] = [
                _omit_null_properties(row) if (row := as_dict_or_none(r)) is not None else r for r in rows
            ]
        else:
            clean[key] = item
    return clean


def usage_counts(params: dict[str, Any]) -> tuple[dict[str, int], int] | None:
    """Return the last response usage and cumulative thread token count."""
    usage = as_dict_or_none(params.get("tokenUsage"))
    if usage is None:
        return None
    last, total = as_dict_or_none(usage.get("last")), as_dict_or_none(usage.get("total"))
    if last is None or total is None:
        return None
    keys = (
        "inputTokens",
        "outputTokens",
        "cachedInputTokens",
        "cacheWriteInputTokens",
    )
    # -1 stands for anything that is not a JSON integer, so it fails the check below.
    values = [as_int(last.get(key, 0), -1) for key in keys]
    cumulative = as_int(total.get("totalTokens"), -1)
    if any(value < 0 for value in values):
        return None
    if cumulative < 0:
        return None
    return {
        "input_tokens": values[0],
        "output_tokens": values[1],
        "cached_input_tokens": values[2],
        "cache_write_input_tokens": values[3],
    }, cumulative


def _evidence_id(value: object) -> str | None:
    return value if isinstance(value, str) and _EVIDENCE_ID.fullmatch(value) else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _error_ok(value: object) -> bool:
    """`null` or an object with a string `message` (`TurnError`, `McpToolCallError`)."""
    error = as_dict_or_none(value)
    return value is None or (error is not None and isinstance(error.get("message"), str))


def _item_problem(item: dict[str, Any] | None) -> str | None:
    """A `ThreadItem` missing its id or type, or an `mcpToolCall` off its schema: required
    `arguments`, `id`, `server`, `status`, `tool`, `type`; nullable `error`, `result` and
    `durationMs` of their declared types; a `completed` status with an error or no result."""
    if item is None or _evidence_id(item.get("id")) is None or not isinstance(item.get("type"), str):
        return "an item has no valid id or type"
    if item["type"] != "mcpToolCall":
        return None
    if (
        "arguments" not in item
        or not isinstance(item.get("server"), str)
        or not isinstance(item.get("tool"), str)
    ):
        return "an mcpToolCall lacks arguments, server or tool"
    if item.get("status") not in _MCP_STATUSES or not _error_ok(item.get("error")):
        return "an mcpToolCall has an invalid status or error"
    if item.get("durationMs") is not None and not _is_int(item.get("durationMs")):
        return "an mcpToolCall has an invalid durationMs"
    result = item.get("result")
    if result is not None and as_list_or_none((as_dict_or_none(result) or {}).get("content")) is None:
        return "an mcpToolCall result has no content list"
    if item["status"] == "completed" and (item.get("error") is not None or result is None):
        return "an mcpToolCall is completed with an error or without a result"
    return None


def _turn_problem(turn: dict[str, Any] | None) -> str | None:
    """A `Turn` off its schema: required `id`, `items` (array) and `status`; nullable `error`."""
    if turn is None or _evidence_id(turn.get("id")) is None:
        return "a turn has no valid id"
    if turn.get("status") not in _TURN_STATUSES or not isinstance(turn.get("items"), list):
        return "a turn has an invalid status or items"
    if not _error_ok(turn.get("error")):
        return "a turn has an invalid error"
    return None


def mcp_result_evidence(
    out_path: Path, *, thread_id: str, server: str, tool: str, needle: str
) -> dict[str, Any]:
    """Whether a dispatch's `.out` notification frames hold exactly one completed `mcpToolCall`
    of `server`/`tool` on `thread_id`, with no error, a text content block containing `needle`,
    in a turn whose only completion is `completed` with no error.

    Every consumed frame (`turn/started`, `turn/completed`, `item/started`, `item/completed`) is
    checked against the fields the app-server schema requires of it (codex-cli 0.162.1:
    `generate-json-schema`); items inside a completed turn's `items` count like completed
    items. Fail-closed: a malformed, foreign-thread, contradictory or oversized input is a
    reason, never a match. A byte-identical repeat of an item or turn completion counts once;
    any differing repeat is a contradiction, and so is an item id whose turn, type, server or
    tool differs between its started and completed evidence. Only the reason, a boolean and validated ids come
    back."""
    thread = _evidence_id(thread_id)

    def verdict(reason: str, item: str | None = None, turn: str | None = None) -> dict[str, Any]:
        return {
            "correlated": item is not None,
            "reason": reason,
            "thread_id": thread,
            "item_id": item,
            "turn_id": turn,
        }

    if thread is None:
        return verdict("unavailable: no valid recorded Codex thread id")
    if not needle:
        return verdict("unavailable: no expected value to look for")
    if not out_path.is_file():
        return verdict("unavailable: no dispatch output")
    with out_path.open("rb") as fh:
        data = fh.read(MAX_EVIDENCE_BYTES + 1)
    if len(data) > MAX_EVIDENCE_BYTES:
        return verdict("oversized: dispatch output exceeds the read bound")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return verdict("malformed: dispatch output is not UTF-8")
    if len(lines) > MAX_EVIDENCE_FRAMES:
        return verdict("oversized: too many frames")
    items: dict[str, tuple[str, str, dict[str, Any]]] = {}
    turns: dict[str, tuple[str, dict[str, Any]]] = {}
    identities: dict[str, tuple[object, ...]] = {}

    def same_identity(turn_id: str, item: dict[str, Any]) -> bool:
        """One item id keeps one turn, type, server and tool across its started frame,
        completed frame and its completed turn's items; only its status and result may move."""
        identity = (turn_id, item.get("type"), item.get("server"), item.get("tool"))
        return identities.setdefault(item["id"], identity) == identity

    def add_item(turn_id: str, item: dict[str, Any]) -> bool:
        entry = (turn_id, json.dumps(item, sort_keys=True), item)
        if item["id"] in items and items[item["id"]][:2] != entry[:2]:
            return False
        if not same_identity(turn_id, item):
            return False
        items[item["id"]] = entry
        return True

    for line in lines:
        if not line.strip():
            continue
        try:
            frame = as_dict_or_none(json.loads(line))
        except ValueError:
            return verdict("malformed: a frame is not JSON")
        if frame is None:
            return verdict("malformed: a frame is not an object")
        method = frame.get("method")
        if method not in _THREAD_FRAMES:
            continue
        params = as_dict_or_none(frame.get("params"))
        if params is None:
            return verdict("malformed: a thread frame has no params")
        if params.get("threadId") != thread:
            return verdict("foreign: a thread frame names another thread or none")
        if method in ("turn/started", "turn/completed"):
            turn = as_dict_or_none(params.get("turn"))
            problem = _turn_problem(turn)
            if turn is None or problem is not None:
                return verdict(f"malformed: {problem}")
            if method == "turn/completed":
                key = json.dumps(turn, sort_keys=True)
                done_id = str(turn["id"])
                if done_id in turns and turns[done_id][0] != key:
                    return verdict("conflicting: one turn completed with different content")
                turns[done_id] = (key, turn)
                for inner in as_list(turn["items"]):
                    inner_item = as_dict_or_none(inner)
                    problem = _item_problem(inner_item)
                    if inner_item is None or problem is not None:
                        return verdict(f"malformed: {problem}")
                    if not add_item(done_id, inner_item):
                        return verdict(
                            "conflicting: one item id completed with different content or identity"
                        )
        else:
            item = as_dict_or_none(params.get("item"))
            stamp = "completedAtMs" if method == "item/completed" else "startedAtMs"
            item_turn = _evidence_id(params.get("turnId"))
            if item_turn is None or not _is_int(params.get(stamp)):
                return verdict(f"malformed: an {method} frame lacks a valid turnId or {stamp}")
            problem = _item_problem(item)
            if item is None or problem is not None:
                return verdict(f"malformed: {problem}")
            if item_turn in turns:
                return verdict("conflicting: an item frame after its turn completed")
            if method == "item/started":
                if item["id"] in items:
                    return verdict("conflicting: an item started after it completed")
                if not same_identity(item_turn, item):
                    return verdict("conflicting: one item id started with a different identity")
            elif not add_item(item_turn, item):
                return verdict("conflicting: one item id completed with different content or identity")
    calls = [
        (item_id, turn_id, item)
        for item_id, (turn_id, _, item) in items.items()
        if item.get("type") == "mcpToolCall" and item.get("server") == server and item.get("tool") == tool
    ]
    if not calls:
        return verdict("missing: no completed call of the tool on this thread")
    if len(calls) > 1:
        return verdict("ambiguous: more than one completed call of the tool")
    item_id, turn_id, item = calls[0]
    if item.get("status") != "completed" or item.get("error") is not None:
        return verdict("error: the call did not complete successfully")
    result = as_dict_or_none(item.get("result"))
    content = as_list_or_none(result.get("content")) if result is not None else None
    if not content:
        return verdict("malformed: the call has no result content")
    blocks = [as_dict_or_none(b) for b in content]
    if any(b is None for b in blocks):
        return verdict("malformed: a result content block is not an object")
    if not any(
        b is not None
        and b.get("type") == "text"
        and isinstance(text := b.get("text"), str)
        and needle in text
        for b in blocks
    ):
        return verdict("mismatch: no text content block carries the expected value")
    done = turns.get(turn_id)
    if done is None or done[1]["status"] != "completed":
        return verdict("incomplete: the call's turn did not complete")
    if done[1].get("error") is not None:
        return verdict("error: the call's turn completed with an error")
    return verdict("matched", item_id, turn_id)


def result_envelope(
    *,
    session_id: str,
    text: str | None,
    cost_usd: float | None,
    num_turns: int,
    status: str,
    denials: list[str],
) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success" if status == "completed" else status,
        "is_error": status != "completed",
        "session_id": session_id,
        "total_cost_usd": cost_usd or 0.0,
        "cost_source": "local-estimate" if cost_usd is not None else "unknown",
        "num_turns": num_turns,
        "result": text or "",
        "structured_output": structured_return(text),
        "terminal_reason": status,
        "permission_denials": denials,
    }
