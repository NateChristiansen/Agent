# ruff: noqa
from __future__ import annotations
import datetime as dt
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from .. import launcher
from ..core import AgentError
from ..records.narrow import as_dict_or_none, dict_list, dig
from .handoffs import transcript_path
from .state import load, one_line


def attach_hint(root: Path, *, dispatch_id: str) -> str:
    d = load(root, dispatch_id)
    if not d.get("session_id"):
        raise AgentError(f"dispatch {dispatch_id} has no session id yet; it has not started")
    return f"cd {d['worktree']} && claude --resume {d['session_id']}"


def watch_command(root: Path, *, dispatch_id: str, digest: bool = False) -> list[str]:
    load(root, dispatch_id)
    argv = [*launcher.argv_prefix(root), "dispatch", "watch", dispatch_id, "--follow"]
    if digest:
        argv.append("--digest")
    return argv


def watch(root: Path, *, dispatch_id: str, follow: bool = False, digest: bool = False) -> Iterator[str]:
    d = load(root, dispatch_id)
    path = transcript_path(d)
    roots = tuple((p for p in (str(d.get("worktree") or ""), str(root)) if p))
    state: dict[str, Any] = {}
    seen = 0
    yielded_wait = False
    while True:
        if path is not None and path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[seen:]:
                yield from (_digest_line_text(line, state, roots) if digest else _transcript_line_text(line))
            seen = len(lines)
        elif not yielded_wait:
            yielded_wait = True
            yield (
                f"(no transcript yet at {path}; the worker has not written anything)"
                if path
                else "(no transcript path known)"
            )
        d = load(root, dispatch_id)
        if not follow or d["state"] not in ("queued", "running"):
            if follow:
                yield f"--- dispatch {dispatch_id} {d['state']}"
            return
        if d.get("transcript_path") and (path is None or Path(d["transcript_path"]) != path):
            path, seen = (Path(d["transcript_path"]), 0)
        time.sleep(1.0)


def _blocks(content: object) -> list[dict[str, object]]:
    blocks = dict_list(content)
    return blocks if isinstance(content, list) else [{"type": "text", "text": content}]


def _result_text(content: object) -> str:
    blocks = dict_list(content)
    if isinstance(content, list):
        return " ".join((str(b.get("text", "")) for b in blocks))
    return str(content or "")


def _transcript_line_text(line: str) -> list[str]:
    try:
        rec = as_dict_or_none(json.loads(line))
    except json.JSONDecodeError:
        return []
    if rec is None:
        return []
    kind = rec.get("type")
    content = dig(rec, "message", "content")
    out: list[str] = []
    if kind == "assistant":
        for block in _blocks(content):
            if block.get("type") == "text" and block.get("text"):
                out.append(f"assistant: {block['text']}")
            elif block.get("type") == "tool_use":
                out.append(f"tool: {block.get('name')} {json.dumps(block.get('input') or {})[:200]}")
    elif kind == "user":
        for block in dict_list(content):
            if block.get("type") == "tool_result":
                out.append(f"result: {one_line(_result_text(block.get('content')))[:200]}")
    return out


DIGEST_TEXT_CHARS = 160
DIGEST_ARG_CHARS = 100
_DIGEST_ARG_KEYS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("pattern",),
    "Glob": ("pattern",),
    "Task": ("description",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "TodoWrite": (),
}


def _shorten_path(text: str, roots: tuple[str, ...]) -> str:
    for root in roots:
        if root and root in text:
            text = text.replace(root.rstrip("/") + "/", "").replace(root, ".")
    return text


def _digest_arg(name: str, inp: Any, roots: tuple[str, ...]) -> str:
    fields = as_dict_or_none(inp)
    if fields is None:
        return ""
    keys = _DIGEST_ARG_KEYS.get(name)
    if keys is None:
        keys = tuple((k for k in ("file_path", "path", "command", "pattern", "query", "url") if k in fields))
    for key in keys:
        value = fields.get(key)
        if isinstance(value, str) and value:
            return _shorten_path(value, roots)[:DIGEST_ARG_CHARS]
    return ""


def _digest_clock(rec: dict[str, Any], state: dict[str, Any]) -> str:
    raw = rec.get("timestamp")
    if not isinstance(raw, str):
        return "[--:--]"
    try:
        when = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "[--:--]"
    origin = state.setdefault("origin", when)
    seconds = max(0, int((when - origin).total_seconds()))
    return f"[{seconds // 60:02d}:{seconds % 60:02d}]"


def _digest_line_text(line: str, state: dict[str, Any], roots: tuple[str, ...]) -> list[str]:
    try:
        rec = as_dict_or_none(json.loads(line))
    except json.JSONDecodeError:
        return []
    if rec is None or rec.get("type") not in ("assistant", "user"):
        return []
    kind = rec["type"]
    stamp = _digest_clock(rec, state)
    content = dig(rec, "message", "content")
    out: list[str] = []
    for block in _blocks(content):
        btype = block.get("type")
        if kind == "assistant" and btype == "text" and block.get("text"):
            out.append(f"{stamp} say   {one_line(str(block['text']))[:DIGEST_TEXT_CHARS]}")
        elif kind == "assistant" and btype == "tool_use":
            name = str(block.get("name") or "?")
            arg = _digest_arg(name, block.get("input"), roots)
            out.append(f"{stamp} {name.lower()[:5]:<5} {one_line(arg)}".rstrip())
        elif kind == "user" and btype == "tool_result" and block.get("is_error"):
            out.append(f"{stamp} FAIL  {one_line(_result_text(block.get('content')))[:DIGEST_TEXT_CHARS]}")
    return out
