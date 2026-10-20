# ruff: noqa
from __future__ import annotations
import re
from dataclasses import dataclass
from functools import lru_cache

TASKS_HEADING = "Tasks"
ITEM_LINE_RE = re.compile("^(\\s*(?:[-*+]|\\d{1,9}[.)])\\s+\\[)([^\\]])(\\]\\s*)(.*)$")


@dataclass(frozen=True)
class Item:
    line_no: int
    end_line: int
    marker: str
    text: str


def is_tasks_heading(name: str) -> bool:
    return name == TASKS_HEADING or name.startswith(TASKS_HEADING + " ")


def split_lines(body: str) -> list[str]:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


@lru_cache(maxsize=256)
def _md():
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").disable("inline")


@lru_cache(maxsize=512)
def scan(body: str) -> tuple[Item, ...]:
    lines = split_lines(body)
    items: list[Item] = []
    in_tasks = False
    tokens = _md().parse(body)
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            title = tokens[i + 1].content.strip() if i + 1 < len(tokens) else ""
            in_tasks = is_tasks_heading(title)
        elif in_tasks and token.type == "list_item_open" and (token.level == 1) and token.map:
            start, end = token.map
            m = ITEM_LINE_RE.match(lines[start]) if start < len(lines) else None
            if m:
                items.append(Item(start, end, m.group(2), m.group(4).strip()))
    return tuple(items)


def tasks_section_end(body: str) -> int | None:
    tokens = _md().parse(body)
    start: int | None = None
    for i, token in enumerate(tokens):
        if token.type != "heading_open" or not token.map:
            continue
        title = tokens[i + 1].content.strip() if i + 1 < len(tokens) else ""
        if start is not None:
            return token.map[0]
        if is_tasks_heading(title):
            start = token.map[0]
    return None if start is None else len(split_lines(body))


def tasks_heading_line(body: str) -> int | None:
    tokens = _md().parse(body)
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.map:
            title = tokens[i + 1].content.strip() if i + 1 < len(tokens) else ""
            if is_tasks_heading(title):
                return token.map[0]
    return None
