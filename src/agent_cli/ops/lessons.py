from __future__ import annotations

from pathlib import Path

from .. import lesson_upstream, lessons
from ._shared import Op


def lesson_add(
    root: Path,
    *,
    title: str,
    scope: str | None = None,
    kind: str = "lesson",
    source: str = "",
    evidence: str,
    home: str = "",
) -> Op:
    entry = lessons.add(
        root, title=title, scope=scope, kind=kind, source=source, evidence=evidence, home=home
    )
    return Op(entry.as_dict(), f"queued {entry.id}: {entry.title}")


def lesson_list(root: Path, *, scope: str | None = None) -> Op:
    del scope
    entries = lessons.open_entries(root)
    items = [e.as_dict() for e in entries]
    lines = [f"{e.id}  {e.fields.get('kind', ''):<9} {e.title}" for e in entries]
    text = "\n".join([*lines, f"{len(entries)} open"]) if entries else "lesson queue is empty"
    return Op({"items": items, "count": len(items)}, text)


def lesson_resolve(root: Path, *, entry_id: str, disposition: str, note: str) -> Op:
    entry = lessons.resolve(root, entry_id=entry_id, disposition=disposition, note=note)
    return Op(entry.as_dict(), f"resolved {entry.id}: {entry.fields['disposition']}")


def lesson_draft_check(root: Path, *, title: str, body_file: str) -> Op:
    result = lesson_upstream.draft_check(root, title=title, body_file=body_file)
    lines = [f"upstream: {result['upstream']} (parent lookup {result['parent_lookup']})"]
    if result["fallback_url"]:
        lines.append(
            f"fallback URL{' (body truncated)' if result['truncated'] else ''}: {result['fallback_url']}"
        )
    return Op(result, "\n".join(lines))
