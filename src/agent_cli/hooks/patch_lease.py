"""Guard every Codex patch path, including added, removed and moved files. Check generated-file and
product-repo protection before vault leases so denied patches take no lease. Codex does not load
Claude native Edit rules; use the same protected-path registry as the Bash guard.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .. import core, leases, sessions
from ..records.narrow import as_dict_or_none
from .guards import lease_guard, protect_generated
from .notes_paths import OWNER_FILES, generated_denial, lexical_vault_path, notes_logical
from .paths import (
    PRODUCT_CODE_REASON,
    PROTECTED_PATH_RE,
    check_path,
    deny,
    relative,
    session_feature,
    slashes,
    under_registered_repo,
)

PATCH_WRITE_REASON = (
    "patches to generated files are not allowed. Use the agent MCP tools instead: log, "
    "status_render, task_add / task_done, feature_use."
)


def patch_targets(patch: str) -> list[str]:
    """Every path the patch names, as written: added, updated, deleted and move targets."""
    if not patch.startswith("*** Begin Patch\n") or "*** End Patch" not in patch:
        return []
    paths: dict[str, None] = {}
    for line in patch.splitlines()[1:]:
        if line == "*** End Patch":
            break
        for marker in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
            if line.startswith(marker):
                paths[line[len(marker) :].strip()] = None
                break
    return list(paths)


def patch_paths(root: Path, patch: str) -> list[str]:
    """The patch's paths inside the vault, relative to it."""
    paths: dict[str, None] = {}
    for raw in patch_targets(patch):
        rel = relative(root, raw)
        if os.path.isabs(rel):
            rel = notes_logical(root, rel) or ""  # a linked notes entry is the vault's
        if rel:
            paths[rel] = None
    return list(paths)


def patch_path_guard(root: Path, payload: dict[str, Any]) -> str:
    """Deny a patch that writes a CLI-owned file or into a registered product repo."""
    data = as_dict_or_none(payload.get("tool_input") or {})
    if payload.get("tool_name") != "apply_patch" or data is None:
        return ""
    for raw in patch_targets(str(data.get("command") or "")):
        if under_registered_repo(root, raw):
            return deny(PRODUCT_CODE_REASON)
        # Lexical, not resolved: in a linked vault `features/` and `journal/` are symlinks into
        # the notes repo, so the resolved path of features/NNN/log.md leaves the root and a
        # root-relative check would skip it. The regex is anchored at the file's tail, so the
        # normalized path, relative or absolute, is enough. `normpath` spells it with `\` on
        # Windows, which the `/`-only regex never matched.
        lexical = slashes(os.path.normpath(raw if os.path.isabs(raw) else os.path.join(root, raw)))
        if PROTECTED_PATH_RE.search(lexical):
            return deny(f"{raw} is CLI-owned; {PATCH_WRITE_REASON}")
        rel = relative(root, raw)
        if not os.path.isabs(rel):
            denial = check_path(rel)
            if denial:
                return denial
        resolved = rel if not os.path.isabs(rel) else notes_logical(root, rel)
        # Judged by the vault path where it lands and as it is spelled: the physical notes
        # spelling, the root-anchored rules (`records/**`) and a symlinked `records/` all escape
        # the lexical regex above. Codex cannot answer an ask, so the owner's files are refused,
        # a hard-linked one included.
        for logical in (resolved, lexical_vault_path(root, raw)):
            if logical is None:
                continue
            if denial := generated_denial(logical):
                return denial
            if logical.lower() in OWNER_FILES:
                return deny(f"{raw} is the owner's file ({logical}); ask the owner to change it")
    return ""


def _area(root: Path, rel: str, session_id: str) -> str | None:
    area = leases.area_for_path(rel)
    exact = leases.get(root, f"vault:{rel}")
    if exact is not None and (area is None or exact.get("session_id") == session_id):
        return f"vault:{rel}"
    return area


def patch_lease_guard(root: Path, payload: dict[str, Any]) -> str:
    """Deny the whole patch when another live session holds any edited vault path."""
    if payload.get("tool_name") != "apply_patch":
        return ""
    session_id = str(payload.get("session_id") or "")
    data = as_dict_or_none(payload.get("tool_input") or {})
    if not session_id or data is None:
        return ""
    try:
        paths = patch_paths(root, str(data.get("command") or ""))
        pid = sessions.resolve_pid()  # None when unknown: a lease reads that, 0 would be malformed
        # Check every path before taking any lease. A denied multi-file patch must not leave
        # a fresh hold on an earlier, otherwise free path.
        for rel in paths:
            area = _area(root, rel, session_id)
            if area is None:
                continue
            existing = leases.blocker(root, area, session_id)
            if existing is not None:
                held = str(existing.get("area") or area)
                leases.request(root, held, session_id=session_id, pid=pid)
                return deny(leases.refusal(root, held, existing))
        context = ""
        for rel in paths:
            if leases.area_for_path(rel) is None:
                area = _area(root, rel, session_id)
                if area is not None:
                    try:
                        leases.take(
                            root,
                            area,
                            session_id=session_id,
                            pid=pid,
                            feature=session_feature(root, session_id),
                        )
                    except core.AgentError as exc:
                        return deny(str(exc))  # another manager took it after the preflight
                continue
            result = lease_guard(root, payload, rel)
            if (
                result
                and json.loads(result).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            ):
                return result  # another manager took the lease after the preflight
            context = context or result
        return context
    except Exception as exc:  # noqa: BLE001 — lease coordination must fail open
        sys.stderr.write(f"note: patch lease check failed ({exc}); allowing the edit\n")
        return ""


def protect_with_patch_leases(root: Path, payload: dict[str, Any]) -> str:
    """The existing protect-generated hook, with a Codex apply_patch branch: paths, then leases."""
    if payload.get("tool_name") == "apply_patch":
        return patch_path_guard(root, payload) or patch_lease_guard(root, payload)
    return protect_generated(root, payload)
