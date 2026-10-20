# ruff: noqa
from __future__ import annotations
import re
from collections.abc import Callable
from pathlib import Path
from ..core import agent_display, agent_path

BUDGET_BYTES = 9000
DIGEST_BYTES = 2500
DIGEST_ROW_CHARS = 110
SPILL_ORDER = ("unassigned", "journal", "retro", "notes", "dirty", "digest", "others", "pending")
_CELL_SPLIT = re.compile("(?<!\\\\)\\|")


def size(text: str) -> int:
    return len(text.encode("utf-8"))


def clip(text: str, limit: int) -> str:
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
    safe = re.sub("[^A-Za-z0-9._-]", "_", session_id) or "no-session"
    return agent_path(root, "scratch", "session-start", f"{safe}.md")


def full_list_display(session_id: str) -> str:
    safe = re.sub("[^A-Za-z0-9._-]", "_", session_id) or "no-session"
    return agent_display("scratch", "session-start", f"{safe}.md")


def write_full_lists(root: Path, session_id: str, sections: list[tuple[str, str]]) -> bool:
    body = "# session-start, full lists\n\n" + "\n\n".join(
        (f"## {name}\n\n{text.strip()}" for name, text in sections if text.strip())
    )
    path = full_list_path(root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def status_digest(status_text: str, limit: int = DIGEST_BYTES) -> str:
    lines: list[str] = []
    in_open = False
    for raw in status_text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_open = line.startswith("## Open features")
            lines.append(line)
        elif in_open and line.startswith("| ["):
            cells = [c.strip() for c in _CELL_SPLIT.split(line.strip().strip("|"))]
            num = re.match("\\[(\\d+)\\]", cells[0])
            if len(cells) >= 8 and num:
                row = f"{num.group(1)} {cells[1]} [{cells[2]}] next: {cells[7]}"
            else:
                row = line
            lines.append("  " + (row if len(row) <= DIGEST_ROW_CHARS else row[: DIGEST_ROW_CHARS - 1] + "…"))
        elif in_open and line and (not line.startswith(("|", "<!--"))):
            lines.append(line)
        elif line and (not line.startswith(("|", "#", "<!--"))) and (not in_open):
            lines.append(line)
    return clip("\n".join(lines), limit)


def fit(
    parts: dict[str, str], assemble: Callable[[dict[str, str], str], str], root_note: str
) -> tuple[str, list[str]]:
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
    return (text, moved)
