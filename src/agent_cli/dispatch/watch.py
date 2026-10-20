"""Looking at a running or finished worker: the `claude --resume` argv, the transcript tail,
and the digest render of it."""

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

# ------------------------------------------------------------------ public: attach / watch


def attach_hint(root: Path, *, dispatch_id: str) -> str:
    """Give the owner a copyable resume command in every state, including blocked and finished work.
    Change to the worker worktree so its repository instructions, skills and hooks load; session
    lookup itself does not require that directory.
    """
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
    """Yield the worker's transcript as text lines. With `follow`, keep tailing while the
    dispatch is running. With `digest`, one short line per event instead of the full text.
    See `_digest_line_text` for why that is the difference between a tail a manager session
    can afford to run in the background and one it cannot."""
    d = load(root, dispatch_id)
    path = transcript_path(d)
    roots = tuple(p for p in (str(d.get("worktree") or ""), str(root)) if p)
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
            path, seen = Path(d["transcript_path"]), 0
        time.sleep(1.0)


def _blocks(content: object) -> list[dict[str, object]]:
    """Content that is not a list is wrapped as one text block."""
    blocks = dict_list(content)
    return blocks if isinstance(content, list) else [{"type": "text", "text": content}]


def _result_text(content: object) -> str:
    blocks = dict_list(content)
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in blocks)
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


# ------------------------------------------------------------------ digest mode

DIGEST_TEXT_CHARS = 160
DIGEST_ARG_CHARS = 100

# The one input worth printing per tool. A digest line is glanced at in a narrow pane while a
# worker runs, so it names WHAT the worker touched, never how.
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
    """Drop the long absolute prefixes so a digest line fits a narrow pane. Both roots matter.
    The worktree is where the work happens, and the vault is on the worker's --add-dir path.
    Without this, a handoff it reads would eat 70 characters of the line."""
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
        keys = tuple(k for k in ("file_path", "path", "command", "pattern", "query", "url") if k in fields)
    for key in keys:
        value = fields.get(key)
        if isinstance(value, str) and value:
            return _shorten_path(value, roots)[:DIGEST_ARG_CHARS]
    return ""


def _digest_clock(rec: dict[str, Any], state: dict[str, Any]) -> str:
    """[mm:ss] since the first record, so a reader sees pace without absolute times."""
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
    """One short line per worker event: what it said, what it ran, what failed.

    SUCCESSFUL tool results are dropped entirely. They are the bulk of a transcript and
    almost none of its meaning.

    Measured on a 65-turn review dispatch. The raw stream the supervisor captures is
    625KB. The plain `watch` render of it is 23.7KB (it already truncates every result to 200
    chars), and the digest is 5.9KB. So the saving over plain `watch` is ~4x, not the ~25x the
    raw stream size suggests. Plain `watch` was never the expensive thing. The reason to
    prefer digest for a background tail: a background task's output returns to the manager
    when it ends, and 1.5k tokens of "what the worker did" beats 6k tokens that include every
    command's first 200 characters.
    """
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
