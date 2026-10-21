"""The lesson queue: findings that have no open feature to hold them (see ADR-0028).

`/lesson` and `/retro` produce findings after a feature has closed. A feature task would
reopen it, so a finding with no open feature waits here until the owner reviews the queue.
Every install keeps one local queue, `harness/owner/lessons-queue.md` in its notes repo
(see ADR-0048), whatever the install's role. `/lesson review` then sends each entry where it
belongs: an upstream issue, a config change, a product repo, or a local fix. The file is
created on first write, because an existing notes repo never receives a new example file
from `notes link`.

The file has `## Open` and `## Resolved`; only Open counts toward the session-start nudge.
New ids read `L-YYYYMMDD-<tag>-NN` with this machine's dispatch label (ADR-0036), so two
machines sharing one notes repo never mint the same id; without a label they fall back to the
legacy `L-YYYYMMDD-NN`. Both forms stay valid. Ids are allocated per machine, so every add and
resolve takes one lock, and `resolve` refuses an id that names two entries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .core import AgentError, atomic_write, today

#: Accepted on `add` and `list` so older callers keep working; it no longer picks a file.
SCOPES = ("harness", "private")
QUEUE = Path("harness/owner/lessons-queue.md")
KINDS = ("lesson", "practice")
DISPOSITIONS = ("task", "mechanism", "prose", "practice", "dropped", "upstream", "config", "repo")
#: Open entries that put a line in the session-start block and refuse `feature close`.
NUDGE_THRESHOLD = 10
LOCK_AREA = "lessons:queue"

_HEADER = (
    "# Lesson queue\n\n"
    "Findings with no open feature to hold them. Written by `agent lesson add`,\n"
    "reviewed with `/lesson review`, which sends each to an upstream issue, a config change, a\n"
    "product repo or a local fix.\n"
)
_FIELDS = ("origin", "kind", "source", "found", "evidence", "home", "resolved", "disposition")
#: The tag is dispatch's `TAG_PATTERN` (ADR-0036), spelled out to keep `dispatch` unimported.
_ENTRY = re.compile(r"^### (L-\d{8}-(?:[a-z][a-z0-9]{0,7}-)?\d{2,}) — (.*)$")
_FIELD = re.compile(r"^- (\w+): ?(.*)$")
_SECTIONS = ("## Open", "## Resolved")


@dataclass
class Entry:
    id: str
    title: str
    fields: dict[str, str] = field(default_factory=dict[str, str])
    resolved: bool = False
    #: Lines under the entry that are not a known field: an owner's hand-written note. Kept
    #: verbatim and written back after the fields, so a mutation never deletes them.
    extra: list[str] = field(default_factory=list[str])

    def render(self) -> str:
        lines = [f"### {self.id} — {self.title}"]
        lines += [f"- {k}: {self.fields[k]}" for k in _FIELDS if self.fields.get(k)]
        lines += self.extra
        return "\n".join(lines).rstrip()

    def as_dict(self) -> dict[str, str]:
        state = "resolved" if self.resolved else "open"
        return {**self.fields, "id": self.id, "title": self.title, "state": state}


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _parse_text(text: str | None) -> tuple[str, list[Entry], list[str]]:
    """`(preamble, entries, stray)` for one queue's text, None for a missing file. The preamble
    is everything above the first `## ` heading, kept as the owner left it. `stray` holds
    non-blank lines the writer could not put back: text between a section heading and its first
    entry, or under a section other than Open and Resolved. A mutation refuses rather than drop
    them."""
    if text is None:
        return _HEADER, [], []
    preamble: list[str] = []
    entries: list[Entry] = []
    stray: list[str] = []
    section: str | None = None
    current: Entry | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line.strip()
            current = None
            if section not in _SECTIONS:
                stray.append(line)
            continue
        if section is None:
            preamble.append(line)
            continue
        m = _ENTRY.match(line)
        if m and section in _SECTIONS:
            current = Entry(m.group(1), m.group(2).strip(), resolved=section == "## Resolved")
            entries.append(current)
            continue
        f = _FIELD.match(line)
        if current is not None and f and f.group(1) in _FIELDS:
            current.fields[f.group(1)] = f.group(2).strip()
        elif current is not None:
            current.extra.append(line)
        elif line.strip():
            stray.append(line)
    for e in entries:  # the blank line that separates entries is layout, not a note
        while e.extra and not e.extra[-1].strip():
            e.extra.pop()
    head = "\n".join(preamble).rstrip() + "\n"
    return (head if head.strip() else _HEADER), entries, stray


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _parse(root: Path) -> tuple[str, list[Entry], list[str]]:
    return _parse_text(_read(root / QUEUE))


def load(root: Path) -> list[Entry]:
    return _parse(root)[1]


def _load_for_write(root: Path) -> tuple[str, list[Entry]]:
    preamble, entries, stray = _parse(root)
    if stray:
        raise AgentError(
            f"{QUEUE} has text the queue writer cannot keep ({stray[0]!r}); move it into the "
            "header or under an entry, then retry"
        )
    return preamble, entries


def _write(root: Path, preamble: str, entries: list[Entry]) -> None:
    open_ = [e.render() for e in entries if not e.resolved]
    done = [e.render() for e in entries if e.resolved]
    parts = [
        preamble,
        "## Open\n",
        *(f"{e}\n" for e in open_),
        "## Resolved\n",
        *(f"{e}\n" for e in done),
    ]
    atomic_write(root / QUEUE, "\n".join(parts))


def open_entries(root: Path) -> list[Entry]:
    return [e for e in load(root) if not e.resolved]


def _next_id(root: Path) -> str:
    """Two machines sharing one notes repo once minted the same id and notes sync kept both;
    the label in the id keeps each machine's sequence apart."""
    from .dispatch import machine

    tag = machine.resolve(root).tag
    prefix = f"L-{today().strftime('%Y%m%d')}-{tag + '-' if tag else ''}"
    taken = {e.id for e in load(root)}
    n = 1
    while f"{prefix}{n:02d}" in taken:
        n += 1
    return f"{prefix}{n:02d}"


def _lock(root: Path):
    from .dispatch.guards import area_guard

    return area_guard(root, LOCK_AREA)


def add(
    root: Path,
    *,
    title: str,
    kind: str = "lesson",
    source: str,
    evidence: str,
    home: str,
    scope: str | None = None,
) -> Entry:
    """Queue a finding locally. `scope` is accepted for older callers and ignored."""
    del scope
    if kind not in KINDS:
        raise AgentError(f"kind must be one of {', '.join(KINDS)}; got {kind!r}")
    values = {
        "title": _one_line(title),
        "source": _one_line(source),
        "evidence": _one_line(evidence),
        "home": _one_line(home),
    }
    for name in ("title", "evidence"):
        if not values[name]:
            raise AgentError(f"{name} is required")
    with _lock(root):
        entry = Entry(
            _next_id(root),
            values["title"],
            {
                "kind": kind,
                "source": values["source"] or "none",
                "found": today().isoformat(),
                "evidence": values["evidence"],
                "home": values["home"],
            },
        )
        preamble, entries = _load_for_write(root)
        entries.append(entry)
        _write(root, preamble, entries)
    return entry


def resolve(root: Path, *, entry_id: str, disposition: str, note: str) -> Entry:
    if disposition not in DISPOSITIONS:
        raise AgentError(f"disposition must be one of {', '.join(DISPOSITIONS)}; got {disposition!r}")
    note = _one_line(note)
    if not note:
        raise AgentError(
            "--note is required: it names the task, commit, file, issue URL or the reason for dropping"
        )
    with _lock(root):
        preamble, entries = _load_for_write(root)
        matches = [e for e in entries if e.id == entry_id]
        if not matches:
            raise AgentError(f"no lesson {entry_id!r} in the queue; see `agent lesson list`")
        if len(matches) > 1:
            titles = "; ".join(e.title for e in matches)
            raise AgentError(
                f"{entry_id} names {len(matches)} entries ({titles}); give one a free id in {QUEUE}, then retry"
            )
        match = matches[0]
        if match.resolved:
            raise AgentError(f"{entry_id} is already resolved: {match.fields.get('disposition')}")
        match.resolved = True
        match.fields["resolved"] = today().isoformat()
        match.fields["disposition"] = f"{disposition}: {note}"
        _write(root, preamble, entries)
        return match


def nudge(root: Path) -> str | None:
    total = len(open_entries(root))
    if total < NUDGE_THRESHOLD:
        return None
    return f"lesson queue: {total} open — run /lesson review"


def refuse_full_queue(root: Path) -> None:
    """A nudge only the model reads let the queue reach 139 entries unseen; a refused close
    puts the count in front of the owner."""
    total = len(open_entries(root))
    if total >= NUDGE_THRESHOLD:
        raise AgentError(
            f"lesson queue has {total} open entries (limit {NUDGE_THRESHOLD - 1}); run /lesson review "
            "before closing a feature"
        )
