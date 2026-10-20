"""Create untracked client skill links during setup. Git for Windows can materialise tracked symlinks
as plain text, so setup creates links appropriate to the platform.

The source is `.agents/skills/` plus `vendor/caveman`; Claude loads `.claude/skills/`. Setup uses
relative symlinks on Unix and directory junctions on Windows without requiring Developer Mode or
admin. Ignored links cannot conflict on pull. `ensure` repairs links without raising; `status`
reports them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .core import AgentError

AGENTS_SKILLS = Path(".agents") / "skills"
CLAUDE_SKILLS = Path(".claude") / "skills"
CAVEMAN_LINK = AGENTS_SKILLS / "caveman"
CAVEMAN_TARGET = Path("vendor") / "caveman" / "skills" / "caveman"


def expected(root: Path) -> list[tuple[Path, Path]]:
    """Every (link, target) pair, both relative to `root`.

    `.agents/skills/caveman` points into the submodule, and each skill directory under
    `.agents/skills/` gets a `.claude/skills/` link. A new skill is linked by adding its folder;
    nothing here names skills one by one.
    """
    pairs: list[tuple[Path, Path]] = [(CAVEMAN_LINK, CAVEMAN_TARGET)]
    names = {CAVEMAN_LINK.name}
    src = root / AGENTS_SKILLS
    if src.is_dir():
        for child in src.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir() or is_link(child):
                names.add(child.name)
    pairs += [(CLAUDE_SKILLS / name, AGENTS_SKILLS / name) for name in sorted(names)]
    return pairs


def is_link(path: Path) -> bool:
    """A symlink or a Windows directory junction."""
    return path.is_symlink() or path.is_junction()


def _points_at(link: Path, target: Path) -> bool:
    try:
        return link.resolve(strict=True) == target.resolve(strict=True)
    except OSError:
        return False


def _make(link: Path, target: Path) -> None:
    """A relative symlink, or on Windows a junction when the OS refuses symlinks."""
    from . import links

    links.make(link, target, is_dir=True)


def _clear(link: Path) -> None:
    """Remove a stale link. Never a real file or directory: `ensure` only calls this for a link,
    or for a file proven to be Git's symlink placeholder."""
    if link.is_junction():
        os.rmdir(link)
    else:
        link.unlink()


def is_placeholder(link: Path, target: Path) -> bool:
    """The plain file Git writes for a tracked symlink when `core.symlinks=false`: its whole
    content is the link's relative target path. Anything else is someone's file, not ours."""
    if is_link(link) or not link.is_file():
        return False
    try:
        content = link.read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return False
    expected_rel = os.path.relpath(target, link.parent)
    return content in (expected_rel, expected_rel.replace(os.sep, "/"))


def ensure(root: Path) -> list[dict[str, Any]]:
    """Make every link that is missing or wrong. One row per link: `already`, `created`,
    `replaced` (a stale link, or Git's exact symlink placeholder file), `refused` (a real file
    or directory is in the way; it is kept), `failed`, or `skipped` when the target does not
    exist yet (the submodule not initialised). A skipped link is not made, so it never dangles;
    the submodule step and doctor's vendor row report that cause."""
    rows: list[dict[str, Any]] = []
    for rel_link, rel_target in expected(root):
        link, target = root / rel_link, root / rel_target
        row: dict[str, Any] = {"link": str(rel_link), "target": str(rel_target)}
        if not target.exists():
            rows.append({**row, "state": "skipped", "detail": f"{rel_target} does not exist"})
            continue
        if is_link(link) and _points_at(link, target):
            rows.append({**row, "state": "already"})
            continue
        if not is_link(link) and link.exists() and not is_placeholder(link, target):
            what = "directory" if link.is_dir() else "file"
            rows.append({**row, "state": "refused", "detail": f"{rel_link} is a real {what}; kept"})
            continue
        existed = is_link(link) or link.exists()
        try:
            if existed:
                _clear(link)
            link.parent.mkdir(parents=True, exist_ok=True)
            _make(link, target)
        except (OSError, AgentError) as exc:
            rows.append({**row, "state": "failed", "detail": str(exc)})
            continue
        rows.append({**row, "state": "replaced" if existed else "created"})
    return rows


def status(root: Path) -> list[dict[str, Any]]:
    """Each link as `ok`, `missing`, `wrong` (points elsewhere), `plain` (a file or directory
    where the link belongs, the `core.symlinks=false` signature), `dangling`, or `no-target`
    (the target itself is absent, such as an uninitialised submodule; that is the submodule
    check's to report, not this one's)."""
    rows: list[dict[str, Any]] = []
    for rel_link, rel_target in expected(root):
        link, target = root / rel_link, root / rel_target
        if not target.exists() and not (is_link(link) or link.exists()):
            state = "no-target"
        elif is_link(link):
            if not target.exists() or not link.exists():
                state = "dangling"
            else:
                state = "ok" if _points_at(link, target) else "wrong"
        elif link.exists():
            state = "plain"
        else:
            state = "missing"
        rows.append({"link": str(rel_link), "target": str(rel_target), "state": state})
    return rows
