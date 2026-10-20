"""Public harness ADRs and private work decisions in separate repositories."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from . import features as features_mod
from . import frontmatter, links, notes, templates
from .core import AgentError, NotFound, today

ADR_DIR = "adrs"
DECISION_DIR = "decisions"
Scope = Literal["notes", "harness"]
STATUSES = ("proposed", "accepted", "superseded", "deprecated")
TYPE_TAG = "adr"
FILE_RE = re.compile(r"^(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")


@dataclass
class Adr:
    id: int
    slug: str
    path: Path
    meta: dict[str, Any]
    body: str

    @property
    def number(self) -> str:
        return f"{self.id:04d}"

    def wikilink(self, root: Path) -> str:
        return f"[[{self.path.relative_to(root).with_suffix('').as_posix()}|ADR-{self.number}]]"

    def save(self) -> None:
        frontmatter.dump(self.path, self.meta, self.body)


def adr_dir(root: Path, scope: Scope = "harness") -> Path:
    return root / (ADR_DIR if scope == "harness" else DECISION_DIR)


def notes_ready(root: Path) -> bool:
    notes_root = notes.read_notes_root(root)
    return bool(
        notes_root
        and (notes_root / DECISION_DIR).is_dir()
        and links.points_at(root / DECISION_DIR, notes_root / DECISION_DIR)
    )


def adr_files(root: Path, scope: Scope = "harness") -> list[Path]:
    base = adr_dir(root, scope)
    if not base.exists():
        return []
    files = [p for p in base.iterdir() if p.is_file() and FILE_RE.match(p.name)]
    return sorted(files, key=lambda p: int(p.name[:4]))


def load_file(path: Path) -> Adr:
    m = FILE_RE.match(path.name)
    if not m:
        raise AgentError(f"{path.name} is not an ADR file (expected NNNN-slug.md)")
    meta, body = frontmatter.load(path)
    return Adr(id=int(m.group(1)), slug=m.group(2), path=path, meta=meta, body=body)


def load_all(root: Path, scope: Scope = "harness") -> list[Adr]:
    return [load_file(p) for p in adr_files(root, scope)]


def next_id(root: Path, scope: Scope = "harness") -> int:
    files = adr_files(root, scope)
    return (int(files[-1].name[:4]) + 1) if files else 1


def resolve(root: Path, ref: str | int, scope: Scope = "harness") -> Adr:
    ref = str(ref).strip()
    files = adr_files(root, scope)
    if ref.isdigit():
        wanted = int(ref)
        for p in files:
            if int(p.name[:4]) == wanted:
                return load_file(p)
        raise NotFound(f"no ADR with id {wanted}")
    for p in files:
        if p.stem == ref or p.stem[5:] == ref:
            return load_file(p)
    raise NotFound(f"no ADR matching {ref!r}")


def new(
    root: Path,
    slug: str,
    title: str,
    feature: features_mod.Feature | None = None,
    status: str = "proposed",
    supersedes: int | None = None,
    when: date | None = None,
    scope: Scope = "notes",
) -> Adr:
    if scope not in ("notes", "harness"):
        raise AgentError("scope must be notes or harness")
    if scope == "notes" and not notes_ready(root):
        raise AgentError("private decisions require a linked notes repo; run `agent setup --notes <path>`")
    slug = features_mod.normalize_slug(slug)
    if status not in STATUSES:
        raise AgentError(f"status must be one of {', '.join(STATUSES)}")
    if not title.strip():
        raise AgentError("--title is required and cannot be empty")
    for p in adr_files(root, scope):
        if p.stem[5:] == slug:
            raise AgentError(f"slug {slug!r} already used by {p.name}")
    old: Adr | None = None
    if supersedes is not None:
        old = resolve(root, supersedes, scope)
    aid = next_id(root, scope)
    number = f"{aid:04d}"
    meta = {
        "id": aid,
        "title": title.strip(),
        "status": status,
        "date": when or today(),
        "supersedes": supersedes,
        "superseded_by": None,
        "tags": [TYPE_TAG] if scope == "harness" else [TYPE_TAG, "private-decision"],
        "links": [],
    }
    if scope == "notes":
        meta["feature"] = feature.id if feature else None
    path = adr_dir(root, scope) / f"{number}-{slug}.md"
    frontmatter.dump(path, meta, templates.adr_body(number, title.strip()))
    adr = load_file(path)
    if old is not None:
        old.meta["superseded_by"] = aid
        old.meta["status"] = "superseded"
        features_mod.add_links(old.meta, [adr.wikilink(root)])
        features_mod.add_links(adr.meta, [old.wikilink(root)])
        old.save()
    if feature is not None:
        if scope == "notes":
            features_mod.add_links(adr.meta, [feature.wikilink(root)])
        features_mod.add_links(feature.meta, [adr.wikilink(root)])
        feature.save()
        if scope == "harness":
            features_mod.set_fields(root, feature, add_adr=[aid])
        else:
            features_mod.set_fields(root, feature, add_decision=[aid])
    adr.save()
    from .journal import pending as journal_pending

    journal_pending.record(root, "adr", f"ADR-{number}", adr.meta["title"])
    return load_file(path)
