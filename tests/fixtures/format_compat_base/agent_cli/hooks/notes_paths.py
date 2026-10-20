# ruff: noqa
from __future__ import annotations
import os
import posixpath
import sys
from pathlib import Path, PurePosixPath
from .paths import VAULT_GENERATED_DENY, canon, deny, relative, slashes

OWNER_FILES = ("harness/owner/rules.md", "harness/voice.md")
"The owner's own files (AGENTS.md; `.claude/rules/harness.md`). Native ask rules cover them in\nthe harness, but may not match the notes side of the link, so a mapped spelling is asked here."
GENERATED_REASON = "is CLI-owned (a generated or record file in the notes repo); change it through the typed tools, never by hand"


def notes_logical(root: Path, rel: str) -> str | None:
    try:
        from .. import links, notes

        notes_root = notes.read_notes_root(root)
        if notes_root is None or not os.path.isabs(rel):
            return None
        for entry in notes.entries(notes_root):
            target = slashes(canon(notes_root / entry.path))
            if rel != target and (not rel.startswith(target + "/")):
                continue
            linked = slashes(canon(root / entry.path)) == target or links.is_hard_linked(
                root / entry.path, notes_root / entry.path
            )
            return entry.path + rel[len(target) :] if linked else None
    except Exception:
        return None
    return None


def is_generated(logical: str) -> bool:
    candidate = PurePosixPath(posixpath.normpath(logical))
    for rule in (*VAULT_GENERATED_DENY, "Edit(/journal/*.collect.*)"):
        pattern = rule.removeprefix("Edit(/").removesuffix(")")
        if candidate.full_match(pattern, case_sensitive=False):
            return True
    return False


def generated_denial(logical: str) -> str:
    return deny(f"{logical} {GENERATED_REASON}") if is_generated(logical) else ""


def vault_path(root: Path, raw: str) -> str | None:
    rel = relative(root, _expand_pwd(root, raw))
    if os.path.isabs(rel):
        return notes_logical(root, rel)
    return rel or None


def lexical_vault_path(root: Path, raw: str) -> str | None:
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
    for logical in (vault_path(root, raw), lexical_vault_path(root, raw)):
        if logical is not None and is_generated(logical):
            return True
    return False


def _expand_pwd(root: Path, raw: str) -> str:
    for var in ("${PWD}", "$PWD"):
        if raw == var or raw.startswith(var + "/"):
            return str(root) + raw[len(var) :]
    return raw
