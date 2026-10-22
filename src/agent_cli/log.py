"""Append-only feature log: features/NNN-slug/log.md.

Entries are Obsidian callouts typed by kind, so they render as colored blocks in the vault
and stay readable as plain markdown:

    > [!decision] 2026-09-03 11:24
    > chose argparse over typer.
    > second line.

One blank line separates entries. The kind is the callout type; a CSS snippet in
.obsidian/snippets gives each kind its icon and color.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .core import AgentError, append_line, now, stamp
from .features import Feature

KINDS = (
    "note",
    "decision",
    "deviation",
    "surprise",
    "measurement",
    "dead-end",
    "blocker",
    "dispatch",
    "handoff",
)
# Reserve dispatch and handoff log kinds for CLI-generated worker events. Both user-facing front
# ends expose the same MODEL_KINDS; append still accepts internal kinds.
MODEL_KINDS = tuple(k for k in KINDS if k not in ("dispatch", "handoff"))
HEADER_RE = re.compile(r"^> \[!(" + "|".join(KINDS) + r")\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2})$")


@dataclass
class Entry:
    ts: str
    kind: str
    text: str

    @property
    def date(self) -> str:
        return self.ts[:10]


def render_entry(entry: Entry) -> str:
    lines = [f"> [!{entry.kind}] {entry.ts}"]
    for line in entry.text.splitlines():
        lines.append(f"> {_escape(line)}".rstrip())
    return "\n".join(lines) + "\n"


def append(
    feature: Feature, kind: str, text: str, when: datetime | None = None, *, reserved: bool = False
) -> Entry:
    """`reserved` is for the reconciliation ledger's own writer; without it, text that reads as
    a ledger entry is refused, so nothing else can forge one."""
    from .reconcile import is_reserved

    if kind not in KINDS:
        raise AgentError(f"kind must be one of {', '.join(KINDS)}; got {kind!r}")
    text = text.strip("\n")
    if not text.strip():
        raise AgentError("log text cannot be empty")
    if not reserved and is_reserved(text):
        raise AgentError("log text starting with `reconcile ` is reserved; use the `reconcile` tool")
    if not feature.log_path.exists():
        raise AgentError(f"{feature.log_path} is missing; the feature directory is corrupt")
    when = when or now()
    entry = Entry(ts=stamp(when), kind=kind, text=text)
    append_line(feature.log_path, "\n" + render_entry(entry))
    return entry


def _escape(line: str) -> str:
    # A body line starting with "[!" would read as a nested callout. Neutralize it.
    return "\\" + line if line.startswith("[!") else line


def _unescape(line: str) -> str:
    return line[1:] if line.startswith("\\[!") else line


def parse(path: Path) -> list[Entry]:
    """Parse log.md into entries. Raises AgentError on a malformed entry header."""
    entries: list[Entry] = []
    current: Entry | None = None
    lines: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.startswith("> [!"):
            m = HEADER_RE.match(raw)
            if not m:
                raise AgentError(f"{path}:{lineno}: malformed log entry header {raw!r}")
            _flush(entries, current, lines)
            current = Entry(ts=m.group(2), kind=m.group(1), text="")
            lines = []
        elif raw.startswith(">"):
            if current is None:
                raise AgentError(f"{path}:{lineno}: quoted line outside an entry")
            body = raw[1:]
            lines.append(_unescape(body[1:] if body.startswith(" ") else body))
        elif raw.startswith("## "):
            raise AgentError(f"{path}:{lineno}: heading {raw!r}; write the entry as a `> [!kind]` callout")
        elif raw.strip() == "" or raw.startswith("# "):
            _flush(entries, current, lines)
            current, lines = None, []
        else:
            if current is None:
                raise AgentError(f"{path}:{lineno}: text outside an entry {raw!r}")
            raise AgentError(f"{path}:{lineno}: entry line must start with '> ' {raw!r}")
    _flush(entries, current, lines)
    return entries


def _flush(entries: list[Entry], current: Entry | None, lines: list[str]) -> None:
    if current is not None:
        current.text = "\n".join(lines).strip("\n")
        entries.append(current)


def last_entry(feature: Feature) -> Entry | None:
    if not feature.log_path.exists():
        return None
    entries = parse(feature.log_path)
    return entries[-1] if entries else None
