"""Export this machine's local evidence before removing a checkout. Ignored .agent state does not live
in the notes repo.

Copy only allowlisted stores, never scratch, private terminal Python or MCP server state. Follow no
links, including Windows junctions, and write nothing inside the clone. Record size and SHA-256 for
every copied file. Captures and peer messages remain private raw evidence; export does not sanitize
them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .core import AgentError, agent_root, iso, now

SCOPE = (
    "sessions",
    "raw",
    "tabs",
    "peers",
    "leases",
    "dispatches",
    "journal",
    "live.json",
    "status.json",
    "default-feature",
    "notes-root",
    "notes-linked.json",
)
"""What `session export` copies from `.agent/`: the records a retro or a handoff needs."""
MANIFEST = "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _is_link(path: Path) -> bool:
    """A symlink, a Windows junction, or any other Windows reparse point. Junctions are not
    symlinks to `is_symlink`, and following one could copy files from outside `.agent/`."""
    try:
        if path.is_symlink() or path.is_junction():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True  # cannot tell, so do not follow it
    return bool(attributes & _REPARSE_POINT)


def _files(base: Path) -> list[Path]:
    """Every regular file under `base` (or `base` itself), following no link of any kind."""
    if not base.exists() or _is_link(base):
        return []
    if base.is_file():
        return [base]
    out: list[Path] = []
    for folder, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not _is_link(Path(folder) / d)]
        out += [p for name in names if not _is_link(p := Path(folder) / name) and p.is_file()]
    return sorted(out)


def export(root: Path, dest: Path, *, feature_dir: str | None = None) -> dict[str, Any]:
    """Copy the in-scope `.agent/` files to `dest` with a manifest. `dest` must be new or empty
    and outside the clone. `feature_dir` (`001-example-feature`) limits `raw/` to that
    feature's captures; everything else in scope is small and always copied."""
    source = agent_root(root)
    if _is_link(source):
        raise AgentError(f"{source} is a link; export reads only a real .agent folder")
    dest = dest.expanduser().resolve()
    if dest == root.resolve() or dest.is_relative_to(root.resolve()):
        raise AgentError(f"export to a folder outside this clone, not {dest}")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise AgentError(f"{dest} is not an empty folder; pick a new one")
    if not source.is_dir():
        raise AgentError(f"{source} does not exist; nothing to export")
    files: list[Path] = []
    for name in SCOPE:
        if name == "raw" and feature_dir:
            # Check `raw` itself first: a linked `raw/` would make an ordinary-looking feature
            # folder beneath it come from outside `.agent/`.
            if not _is_link(source / "raw"):
                files += _files(source / "raw" / feature_dir)
        else:
            files += _files(source / name)
    rows: list[dict[str, Any]] = []
    for path in files:
        rel = path.relative_to(source)
        target = agent_root(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        rows.append({"path": rel.as_posix(), "bytes": target.stat().st_size, "sha256": _sha256(target)})
    manifest = {
        "exported_at": iso(now()),
        "source": str(root.resolve()),
        "scope": list(SCOPE),
        "feature": feature_dir,
        "files": rows,
    }
    dest.mkdir(parents=True, exist_ok=True)
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"dest": str(dest), "files": len(rows), "bytes": sum(r["bytes"] for r in rows)}
