"""Where a plan's tasks are, read as CommonMark.

The Dataview views count the list items Dataview indexes: a task's section is its nearest
heading, and a top-level item has no parent. Hand-written line rules disagreed with that on valid
Markdown: an item indented one to three spaces is a top-level peer, an empty `###` is still a
heading, a `- [ ]` inside a fenced code block is no task, and `- [ ]` under a `### Detail` inside
`## Tasks` is not in the Tasks section. So `features.list_tasks` takes the structure from
markdown-it (CommonMark, already a dependency), and only reads each task's own first line here.

A task is a top-level list item (bullet or ordered) whose first line is a checkbox, directly
under a heading named `Tasks` or `Tasks …` (as `## Tasks (phase 2)`), up to the next heading of
any level. Nested items are sub-steps. Line numbers count `\\n`, `\\r\\n` and `\\r` breaks only, as
markdown-it does; `split_lines` is the one split every writer uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

TASKS_HEADING = "Tasks"
# A checkbox item's first line: any indent, the list marker, the box, the text.
ITEM_LINE_RE = re.compile(r"^(\s*(?:[-*+]|\d{1,9}[.)])\s+\[)([^\]])(\]\s*)(.*)$")


@dataclass(frozen=True)
class Item:
    """One task line: where it is, its box character and its text."""

    line_no: int  # 0-based, in `split_lines(body)`
    end_line: int  # first line after the whole list item (its sub-steps included)
    marker: str
    text: str


def is_tasks_heading(name: str) -> bool:
    """The views' `isTasks`: `Tasks`, or `Tasks` then a space and anything."""
    return name == TASKS_HEADING or name.startswith(TASKS_HEADING + " ")


def split_lines(body: str) -> list[str]:
    """`body`'s lines as markdown-it numbers them: `\\r\\n`, `\\r` and `\\n` end a line, nothing
    else does (`str.splitlines` also breaks on `\\x0c`, U+2028 and others)."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


@lru_cache(maxsize=256)
def _md():  # noqa: ANN202 - markdown_it is imported lazily
    from markdown_it import MarkdownIt

    # Block structure only: headings, lists and fences are all this needs.
    return MarkdownIt("commonmark").disable("inline")


@lru_cache(maxsize=512)
def scan(body: str) -> tuple[Item, ...]:
    """Every checkbox item that is a task in `body`, in document order, blank ones included."""
    lines = split_lines(body)
    items: list[Item] = []
    in_tasks = False
    tokens = _md().parse(body)
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            title = tokens[i + 1].content.strip() if i + 1 < len(tokens) else ""
            in_tasks = is_tasks_heading(title)
        elif in_tasks and token.type == "list_item_open" and token.level == 1 and token.map:
            start, end = token.map
            m = ITEM_LINE_RE.match(lines[start]) if start < len(lines) else None
            if m:
                items.append(Item(start, end, m.group(2), m.group(4).strip()))
    return tuple(items)


def tasks_section_end(body: str) -> int | None:
    """The line after the first `Tasks` section (the next heading, or the end), or None when the
    plan has no `Tasks` heading."""
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
    """The first `Tasks` heading's line, or None."""
    tokens = _md().parse(body)
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.map:
            title = tokens[i + 1].content.strip() if i + 1 < len(tokens) else ""
            if is_tasks_heading(title):
                return token.map[0]
    return None
