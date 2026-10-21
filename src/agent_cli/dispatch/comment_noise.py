"""Comment noise: what a worker may not add, refused at `git commit` and again at return.

Two kinds are refused. A comment or docstring that cites a feature, lesson or dispatch id is
history a reader of the code does not need. The same text added in three or more files is
boilerplate. A test's Given/When/Then lines, tool directives and docstring section headers are
exempt. Everything else is the reviewer's call: no comment needs a justification row.

Python docstrings are found with `ast`, so only a real module, class or function docstring
counts; an ordinary string literal never does.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from typing import Any

from .conventions import GWT_RE, FileDiff
from .conventions import squash as squash

#: A cited id: `feature NNN` (exactly three digits), a dispatch id with a host part such as
#: `NNN-mac-SS`, or a lesson id `L-YYYYMMDD-N` or `L-YYYYMMDD-mac-N`. Bare `NNN-NN`, `task N`
#: and `dispatch N` read as ordinary prose or numbers, so they pass.
ID_RE = re.compile(
    r"\bfeature\s+\d{3}\b"
    r"|\b\d{3}-[a-z]+-\d{2}\b"
    r"|\bL-\d{8}-(?:[a-z][a-z0-9]*-)?\d+\b",
    re.IGNORECASE,
)
#: Comments a tool reads, which repeat across files by design.
DIRECTIVE_RE = re.compile(
    r"^(?:fmt:|noqa|type:|pragma|pyright:|pylint:|ruff:|mypy:|isort:|eslint|prettier|@ts-|"
    r"istanbul|nolint|-\*-|region\b|endregion\b|ReSharper)",
    re.IGNORECASE,
)
#: Docstring section headers and underlines, which repeat across files by design.
SECTION_RE = re.compile(
    r"^(?:args|arguments|parameters|params|returns?|yields?|raises|attributes|examples?|notes?|"
    r"see also|warnings?|todo)\s*:?$|^[-=]{3,}$",
    re.IGNORECASE,
)
REPEAT_FILES = 3
_QUOTES_RE = re.compile(r'^[rRbBuUfF]{0,2}(?:"""|\'\'\'|"|\')|(?:"""|\'\'\'|"|\')$')


def noisy(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The comments to refuse, each with its `why`: an id it cites, or the files it repeats in."""
    kept = [
        c
        for c in comments
        if not GWT_RE.match(c["text"])
        and not DIRECTIVE_RE.match(c["text"])
        and not SECTION_RE.match(c["text"])
    ]
    paths: dict[str, set[str]] = {}
    for c in kept:
        paths.setdefault(squash(c["text"]), set()).add(c["path"])
    out: list[dict[str, Any]] = []
    for c in kept:
        if m := ID_RE.search(c["text"]):
            out.append({**c, "why": f'cites "{m.group(0)}"'})
        elif len(seen := paths[squash(c["text"])]) >= REPEAT_FILES:
            out.append({**c, "why": f"same text in {len(seen)} files"})
    return out


def docstring_lines(source: str) -> set[int]:
    """The line numbers that belong to a module, class or function docstring in `source`;
    none when it does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError, ValueError:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
        ):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                lines.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return lines


def added_docstrings(files: list[FileDiff], read: Callable[[str], str | None]) -> list[dict[str, Any]]:
    """Every added docstring line in a Python file, consecutive lines joined into one block the
    way `added_comments` joins comment lines. `read(path)` returns the file as the result has it."""
    blocks: list[dict[str, Any]] = []
    for f in files:
        if not f.path.endswith(".py") or not f.added:
            continue
        source = read(f.path)
        inside = docstring_lines(source) if source is not None else set[int]()
        prev = -2
        for line_no, text in f.added:
            body = _QUOTES_RE.sub("", text.strip()).strip() if line_no in inside else None
            if not body:
                prev = -2
                continue
            if blocks and line_no == prev + 1 and blocks[-1]["path"] == f.path:
                blocks[-1]["text"] = (blocks[-1]["text"] + " " + body).strip()
                blocks[-1]["end"] = line_no
            else:
                blocks.append({"path": f.path, "line": line_no, "end": line_no, "text": body})
            prev = line_no
    return blocks
