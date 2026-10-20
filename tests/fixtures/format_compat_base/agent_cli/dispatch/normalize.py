# ruff: noqa
from __future__ import annotations
import dataclasses
from typing import Any
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from ..permissions import shown_tool
from ..records.narrow import as_dict, as_dict_or_none, as_list
from . import usage as usage_mod
from .state import one_line, shown_denials


def return_status(envelope: dict[str, Any]) -> str:
    return str(as_dict(envelope.get("structured_output")).get("status") or "").strip()


def result_envelope(msg: ResultMessage) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": msg.subtype,
        "is_error": bool(msg.is_error),
        "session_id": msg.session_id,
        "total_cost_usd": msg.total_cost_usd,
        "num_turns": msg.num_turns,
        "result": msg.result,
        "structured_output": msg.structured_output,
        "terminal_reason": msg.terminal_reason,
        "permission_denials": shown_denials(_jsonable(msg.permission_denials)),
        "usage": _jsonable(msg.usage),
        "model_usage": _jsonable(msg.model_usage),
    }


def out_record(msg: Any) -> dict[str, Any]:
    if isinstance(msg, ResultMessage):
        return result_envelope(msg)
    if isinstance(msg, AssistantMessage):
        return {
            "type": "assistant",
            "model": msg.model,
            "text": "\n".join((b.text for b in msg.content if isinstance(b, TextBlock))),
            "tools": [shown_tool(b.name) for b in msg.content if isinstance(b, ToolUseBlock)],
            "tokens": usage_mod.message_tokens(msg.usage),
            "message_id": getattr(msg, "message_id", None),
        }
    if isinstance(msg, UserMessage):
        blocks = msg.content if isinstance(msg.content, list) else []
        return {
            "type": "user",
            "text": msg.content if isinstance(msg.content, str) else "",
            "tool_results": len([b for b in blocks if isinstance(b, ToolResultBlock)]),
            "errors": [
                one_line(str(b.content))[:200]
                for b in blocks
                if isinstance(b, ToolResultBlock) and b.is_error
            ],
        }
    if isinstance(msg, SystemMessage):
        return {"type": "system", "subtype": msg.subtype}
    return {"type": type(msg).__name__}


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and (not isinstance(value, type)):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    mapping = as_dict_or_none(value)
    if mapping is not None:
        return {k: _jsonable(v) for k, v in mapping.items()}
    elements = as_list(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in elements]
    return value
