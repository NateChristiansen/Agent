"""Keep manager worktrees under a shared parent covered by client writable grants. Configure grants
for that parent so each new worktree does not require another settings change.

Claude uses an absolute additionalDirectories grant plus Edit/Write allow rules and root-anchored
protected-path ask rules; ask rules take precedence. Codex lists the parent as a writable root in
its generated manager profile. Both clients load grants at session start, so changes require a new
session.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePath
from typing import Any

from .core import AgentError, ExternalFailure, atomic_write
from .records.narrow import as_dict_or_none, as_list_or_none

SETTINGS = Path(".claude") / "settings.json"
SETTINGS_LOCAL = Path(".claude") / "settings.local.json"


def parent_for(root: Path) -> Path:
    """The worktree parent for this checkout, absolute. From a worktree it is still the main
    checkout's sibling, so setup run anywhere grants the same directory."""
    from . import worktree as worktree_mod

    try:
        main = worktree_mod.main_checkout(root)
    except AgentError, ExternalFailure, OSError:
        main = root
    return worktree_mod.worktrees_dir(main.resolve()).resolve()


def _read_settings(root: Path, rel: Path, *, missing_ok: bool) -> tuple[dict[str, Any], str | None]:
    """`(settings, text)` from a Claude settings file, with `permissions`, `permissions.allow`,
    `permissions.ask` and `permissions.additionalDirectories` checked as an object and lists of
    strings.
    Raises AgentError naming what is wrong; a missing file is `({}, None)` when `missing_ok`.
    One reader for the source and the local file: doctor once raised TypeError on a local
    `additionalDirectories = 123` that setup had already refused (reviewer probe)."""
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if missing_ok:
            return {}, None
        raise AgentError(f"cannot read {rel} ({exc})") from exc
    except OSError as exc:
        raise AgentError(f"cannot read {rel} ({exc})") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AgentError(f"{rel} is not valid JSON ({exc})") from exc
    settings = as_dict_or_none(data)
    if settings is None:
        raise AgentError(f"{rel} is not a settings object")
    perms = as_dict_or_none(settings.get("permissions", {}))
    if perms is None:
        raise AgentError(f"{rel} permissions is not an object")
    for key in ("allow", "ask", "additionalDirectories"):
        value = as_list_or_none(perms.get(key, []))
        if value is None or not all(isinstance(v, str) for v in value):
            raise AgentError(f"{rel} permissions.{key} is not a list of strings")
    return settings, text


def protected_paths(root: Path) -> list[str]:
    """The relative paths the checked-in settings ask before editing: each `Edit(<path>)` in
    `permissions.ask`, read from the file so the local copy cannot drift from it. A rule that
    is already absolute (`//`, `~`) covers itself and is skipped.

    Raises AgentError when the source cannot be read or has the wrong shape. Returning `[]`
    there granted the parent with no ask rules and let doctor see no gap; only a readable file with no asks means no rules."""
    try:
        data, _ = _read_settings(root, SETTINGS, missing_ok=False)
    except AgentError as exc:
        raise AgentError(f"{exc.message}; its ask rules are the source") from exc
    out: list[str] = []
    for rule in data.get("permissions", {}).get("ask", []):
        if not (rule.startswith("Edit(") and rule.endswith(")")):
            continue
        rel = rule[len("Edit(") : -1]
        if rel.startswith(("//", "~")):
            continue
        rel = rel.removeprefix("./").removeprefix("/")
        if rel and rel not in out:
            out.append(rel)
    return out


def _absolute(path: PurePath) -> str:
    """A path in Claude's rule syntax: POSIX form, a Windows drive as `/c/...`."""
    posix = path.as_posix()
    if len(posix) > 1 and posix[1] == ":":
        posix = f"/{posix[0].lower()}{posix[2:]}"
    return posix


def ask_rules(parent: Path, paths: list[str]) -> list[str]:
    """`Edit(//<parent>/*/<path>)` per protected path: `//` anchors at the filesystem root and
    `*` is one worktree directory."""
    return [f"Edit(/{_absolute(parent)}/*/{rel})" for rel in paths]


def allow_rules(parent: Path) -> list[str]:
    """`Edit` and `Write` for every file under the parent, so a worktree edit runs unasked."""
    return [f"{tool}(/{_absolute(parent)}/**)" for tool in ("Edit", "Write")]


def notes_allow_rules(notes_root: Path) -> list[str]:
    """`Edit` and `Write` for the notes repo's `features/` and `adrs/`, so a manager records
    work there unasked."""
    base = f"/{_absolute(notes_root)}"
    return [f"{tool}({base}/{sub}/**)" for sub in ("features", "adrs") for tool in ("Edit", "Write")]


def _wanted(root: Path, notes_root: Path | None = None) -> tuple[list[str], list[str], list[str]]:
    parent = parent_for(root)
    allows = allow_rules(parent) + (notes_allow_rules(notes_root) if notes_root else [])
    dirs = [str(parent)] + ([str(notes_root)] if notes_root else [])
    return dirs, allows, ask_rules(parent, protected_paths(root))


def notes_granted(root: Path, notes_root: Path) -> bool:
    """`notes_root` is in the local `permissions.additionalDirectories`. Raises AgentError when
    the local file cannot be read."""
    data, _ = _read_settings(root, SETTINGS_LOCAL, missing_ok=True)
    return str(notes_root) in data.get("permissions", {}).get("additionalDirectories", [])


def configure_claude(root: Path, notes_root: Path | None = None) -> tuple[str, str]:
    """Merge the parent grant, its allow rules and its ask rules into
    `.claude/settings.local.json`, and the linked notes root as a working directory, so a Read
    of `features/` through the link does not prompt. Idempotent.
    Only appends: every key and rule the owner set stays, in its order, and a moved parent or
    notes root is added beside the old entry. Skipped when this clone has no checked-in settings
    (Claude is not set up here); refused, writing nothing, when they cannot be read, so the
    parent is never granted without its ask rules."""
    if not claude_set_up(root):
        return "skipped", f"no {SETTINGS}; Claude is not set up in this clone"
    try:
        grants, allows, rules = _wanted(root, notes_root)
    except AgentError as exc:
        return "refused", f"{exc.message}; the worktree grant was not written"
    path = root / SETTINGS_LOCAL
    if path.is_symlink():
        return "refused", f"{SETTINGS_LOCAL} is a symlink; update it yourself"
    try:
        data, text = _read_settings(root, SETTINGS_LOCAL, missing_ok=True)
    except AgentError as exc:
        return "refused", f"{exc.message}; fix it and run setup again"
    perms = data.get("permissions", {})
    dirs, ask = perms.get("additionalDirectories", []), perms.get("ask", [])
    allow = perms.get("allow", [])
    detail = f"{SETTINGS_LOCAL} grants {grants[0]} with Edit and Write allowed and {len(rules)} ask rule(s)"
    if notes_root:
        detail += f", and the notes repo {grants[1]} as a working directory"
    new_dirs = dirs + [d for d in grants if d not in dirs]
    new_allow = allow + [r for r in allows if r not in allow]
    new_ask = ask + [r for r in rules if r not in ask]
    if text is not None and new_dirs == dirs and new_allow == allow and new_ask == ask:
        return "already", detail
    perms["additionalDirectories"] = new_dirs
    perms["allow"] = new_allow
    if new_ask:
        perms["ask"] = new_ask
    data["permissions"] = perms
    atomic_write(path, json.dumps(data, indent=2) + "\n")
    return "done", detail


def claude_set_up(root: Path) -> bool:
    """The checked-in settings are present (even if unreadable): every real clone."""
    path = root / SETTINGS
    return path.exists() or path.is_symlink()
