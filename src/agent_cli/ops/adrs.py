"""ADR ops: adr_new."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import adrs as adrs_mod
from .. import features as features_mod
from .. import obsidian as obsidian_mod
from ._shared import Op, render_views

# ---------------------------------------------------------------- adr


def _adr_dict(root: Path, a: adrs_mod.Adr) -> dict[str, Any]:
    return {
        "id": a.id,
        "number": a.number,
        "slug": a.slug,
        "file": a.path.relative_to(root).as_posix(),
        "title": a.meta.get("title"),
        "status": a.meta.get("status"),
        "date": a.meta.get("date"),
        "feature": a.meta.get("feature"),
        "supersedes": a.meta.get("supersedes"),
        "superseded_by": a.meta.get("superseded_by"),
        "tags": a.meta.get("tags") or [],
        "links": a.meta.get("links") or [],
        "scope": "harness" if a.path.parent == adrs_mod.adr_dir(root) else "notes",
    }


def adr_new(
    root: Path,
    *,
    slug: str,
    title: str,
    feature: str | None = None,
    status: str = "proposed",
    supersedes: int | None = None,
    session_id: str | None = None,
    scope: adrs_mod.Scope = "notes",
) -> Op:
    feat = features_mod.resolve(root, feature, session_id) if feature else None
    a = adrs_mod.new(root, slug, title, feat, status, supersedes, scope=scope)
    render_views(root)
    rel = a.path.relative_to(root).as_posix()
    obsidian_mod.auto_open(root, rel)
    return Op(_adr_dict(root, a), f"created {rel}")
