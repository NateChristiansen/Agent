"""Map physical linked-note paths back to vault-relative names so generated-file protection uses the
same CLI-owned path list through links and direct paths.
"""

from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path, PurePosixPath

from .paths import VAULT_GENERATED_DENY, canon, deny, relative, slashes

OWNER_FILES = ("harness/owner/rules.md", "harness/voice.md")
"""The owner's own files (AGENTS.md; `.claude/rules/harness.md`). Native ask rules cover them in
the harness, but may not match the notes side of the link, so a mapped spelling is asked here."""

GENERATED_REASON = (
    "is CLI-owned (a generated or record file in the notes repo); change it through the typed "
    "tools, never by hand"
)


def notes_logical(root: Path, rel: str) -> str | None:
    """The vault path an outside-root `rel` (as `relative` returns it) stands for when it lies in
    a linked notes entry: `/…/Agent-notes/features/040-x/plan.md` -> `features/040-x/plan.md`,
    however it was spelled (through the link or the physical path). None when it lies anywhere
    else, or on any failure, so the caller keeps its outside-root ask."""
    try:
        from .. import links, notes

        notes_root = notes.read_notes_root(root)
        if notes_root is None or not os.path.isabs(rel):
            return None
        for entry in notes.entries(notes_root):
            target = slashes(canon(notes_root / entry.path))
            if rel != target and not rel.startswith(target + "/"):
                continue
            # The entry must really be linked from this harness: a stale notes-root file pointing
            # at a repo this clone does not link grants nothing. A hard link (the Windows file
            # fallback) never resolves to its target, so it is compared as the same file.
            linked = slashes(canon(root / entry.path)) == target or links.is_hard_linked(
                root / entry.path, notes_root / entry.path
            )
            return entry.path + rel[len(target) :] if linked else None
    except Exception:  # noqa: BLE001 — a mapping failure keeps the ask; a hook never raises
        return None
    return None


def is_generated(logical: str) -> bool:
    """`logical` (a vault path, root-relative) is one the native `Edit(...)` rules list as
    CLI-owned. Anchored at the vault root, so `src/agent_cli/records/` is not `records/**`."""
    candidate = PurePosixPath(posixpath.normpath(logical))
    for rule in (*VAULT_GENERATED_DENY, "Edit(/journal/*.collect.*)"):
        pattern = rule.removeprefix("Edit(/").removesuffix(")")
        if candidate.full_match(pattern, case_sensitive=False):
            return True
    return False


def generated_denial(logical: str) -> str:
    """Deny a mapped notes path that the native `Edit(...)` rules list as CLI-owned. The native
    rules match the requested spelling at the project root, so they never see the physical path
    in the notes repo; this applies the same list to it."""
    return deny(f"{logical} {GENERATED_REASON}") if is_generated(logical) else ""


def vault_path(root: Path, raw: str) -> str | None:
    """The root-relative vault path `raw` writes, through links and `.`/`..` spellings, or None
    when it lands outside the vault. For the Bash and patch halves, which see a spelled path."""
    rel = relative(root, _expand_pwd(root, raw))
    if os.path.isabs(rel):
        return notes_logical(root, rel)
    return rel or None


def lexical_vault_path(root: Path, raw: str) -> str | None:
    """The root-relative path `raw` names as spelled, links not followed, or None outside the
    root. A symlinked `records/` resolves elsewhere, but the spelled path is still the vault's
    `records/**`, which the native rule protects."""
    spelled = slashes(_expand_pwd(root, raw))
    if not os.path.isabs(spelled):
        rel = posixpath.normpath(spelled)
        return None if rel == ".." or rel.startswith("../") else rel
    base = slashes(os.path.normpath(str(root)))
    target = slashes(os.path.normpath(spelled))
    fold = sys.platform in ("darwin", "win32")
    if (target.lower() if fold else target).startswith((base.lower() if fold else base) + "/"):
        return target[len(base) + 1 :]
    return None


def protected_write(root: Path, raw: str) -> bool:
    """A write to `raw` lands on a CLI-owned vault file, judged by where it resolves and by how
    it is spelled."""
    for logical in (vault_path(root, raw), lexical_vault_path(root, raw)):
        if logical is not None and is_generated(logical):
            return True
    return False


def _expand_pwd(root: Path, raw: str) -> str:
    """`$PWD/x` and `${PWD}/x` as a shell in the harness root reads them."""
    for var in ("${PWD}", "$PWD"):
        if raw == var or raw.startswith(var + "/"):
            return str(root) + raw[len(var) :]
    return raw
