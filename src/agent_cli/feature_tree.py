"""The one walk that finds every feature and initiative in the vault (see ADR-0040).

Standalone features sit at `features/NNN-slug/`. An initiative sits at `initiatives/NN-slug/`
and holds its own `features/NNN-slug/` and child `initiatives/NN-slug/`, to any depth. Feature
ids are one global sequence and initiative ids are another, so the walk refuses a duplicate of
either anywhere in the tree. It also refuses a containment cycle, which only a symlink can make.

Everything that finds features goes through `walk`: `features.feature_dirs`, and through it
`load_all`, `next_id`, `resolve` and the slug check in `new`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .core import AgentError

FEATURES_DIR = "features"
INITIATIVES_DIR = "initiatives"
FEATURE_DIR_RE = re.compile(r"^(\d{3,})-([a-z0-9]+(?:-[a-z0-9]+)*)$")
INITIATIVE_DIR_RE = re.compile(r"^(\d{2,})-([a-z0-9]+(?:-[a-z0-9]+)*)$")


@dataclass(frozen=True)
class InitiativeDir:
    """An initiative folder as the walk found it. `parent` is the containing initiative's id,
    None for one directly under the vault's `initiatives/`."""

    id: int
    slug: str
    dir: Path
    parent: int | None

    @property
    def number(self) -> str:
        return f"{self.id:02d}"


@dataclass(frozen=True)
class Tree:
    features: tuple[Path, ...]
    initiatives: tuple[InitiativeDir, ...]

    @property
    def feature_ids(self) -> set[int]:
        return {int(m.group(1)) for p in self.features if (m := FEATURE_DIR_RE.match(p.name))}


def _children(base: Path, pattern: re.Pattern[str]) -> list[tuple[re.Match[str], Path]]:
    if not base.is_dir():
        return []
    out: list[tuple[re.Match[str], Path]] = []
    for p in base.iterdir():
        m = pattern.match(p.name)
        if m and p.is_dir():
            out.append((m, p))
    return sorted(out, key=lambda pair: pair[1].name)


def _real(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AgentError(f"cannot resolve {path}: {exc}") from exc


def walk(root: Path) -> Tree:
    """Every feature dir and initiative under `root`. Raises AgentError on a duplicate feature
    id, a duplicate initiative id or a containment cycle, naming the paths involved."""
    features: dict[int, Path] = {}
    initiatives: dict[int, InitiativeDir] = {}

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    def visit(container: Path, parent: int | None, chain: tuple[Path, ...]) -> None:
        for m, p in _children(container / FEATURES_DIR, FEATURE_DIR_RE):
            fid = int(m.group(1))
            if fid in features:
                raise AgentError(f"duplicate feature id {fid:03d}: {rel(features[fid])} and {rel(p)}")
            features[fid] = p
        for m, p in _children(container / INITIATIVES_DIR, INITIATIVE_DIR_RE):
            real = _real(p)
            if real in chain:
                raise AgentError(
                    f"containment cycle: {rel(p)} is {real}, which already contains it; "
                    "an initiative cannot sit inside itself"
                )
            iid = int(m.group(1))
            if iid in initiatives:
                raise AgentError(
                    f"duplicate initiative id {iid:02d}: {rel(initiatives[iid].dir)} and {rel(p)}"
                )
            initiatives[iid] = InitiativeDir(iid, m.group(2), p, parent)
            visit(p, iid, (*chain, real))

    visit(root, None, (_real(root),))
    return Tree(
        features=tuple(features[k] for k in sorted(features)),
        initiatives=tuple(initiatives[k] for k in sorted(initiatives)),
    )


def vault_root(feature_dir: Path) -> Path:
    """The vault root a feature dir found by `walk` sits under. Read from the path itself:
    `<root>/features/NNN-slug`, or `<root>/initiatives/NN-a/[initiatives/NN-b/...]features/NNN-slug`."""
    container = feature_dir.parent.parent
    while container.parent.name == INITIATIVES_DIR and INITIATIVE_DIR_RE.match(container.name):
        container = container.parent.parent
    return container


def _entries(paths: list[str]) -> list[tuple[str, re.Match[str], str]]:
    """(kind, name match, folder path) for each feature or initiative folder the vault-relative
    paths name, read the same way `walk` reads the disk. Used on a git tree listing, where
    nothing can be resolved."""
    out: list[tuple[str, re.Match[str], str]] = []
    for path in paths:
        parts = path.split("/")
        i = 0
        while (
            i + 1 < len(parts)
            and parts[i] == INITIATIVES_DIR
            and (m := INITIATIVE_DIR_RE.match(parts[i + 1]))
        ):
            out.append(("initiative", m, "/".join(parts[: i + 2])))
            i += 2
        if i + 1 < len(parts) and parts[i] == FEATURES_DIR and (m := FEATURE_DIR_RE.match(parts[i + 1])):
            out.append(("feature", m, "/".join(parts[: i + 2])))
    return out


def ids_in_paths(paths: list[str]) -> tuple[set[int], set[int]]:
    """(feature ids, initiative ids) named by vault-relative folder paths."""
    entries = _entries(paths)
    return (
        {int(m.group(1)) for kind, m, _ in entries if kind == "feature"},
        {int(m.group(1)) for kind, m, _ in entries if kind == "initiative"},
    )


def feature_slugs_in_paths(paths: list[str]) -> dict[str, str]:
    """Each feature slug the vault-relative folder paths name, with the folder that holds it."""
    return {m.group(2): folder for kind, m, folder in _entries(paths) if kind == "feature"}
