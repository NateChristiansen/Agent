# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
from .core import AgentError
from .records.narrow import as_dict, as_dict_or_none

REGISTRY = "agent.yaml"


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def registry_path(root: Path) -> Path:
    return root / REGISTRY


PLACEHOLDER_OWNER = "Your Name"
"Treat the example owner.name as unset so setup and session-start resolve identity consistently."
EMPTY_REPOS = (
    "# Your repos. agent.example.yaml shows every field; `/agent-config repo` adds one.\nrepos: []\n"
)


def drop_example_repos(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    at = text.find("\nrepos:\n")
    if at >= 0:
        path.write_text(text[: at + 1] + EMPTY_REPOS, encoding="utf-8", newline="\n")


def set_owner_name(root: Path, name: str) -> tuple[str, str]:
    import json

    name = " ".join(name.split())
    if not name:
        return ("skipped", "no name given")
    if owner_name(root):
        return ("already", f"owner is {owner_name(root)}")
    path = registry_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ("failed", f"cannot read {path.name}: {exc}")
    placeholder = f"  name: {PLACEHOLDER_OWNER}\n"
    if placeholder not in text:
        return (
            "skipped",
            f"no `owner.name: {PLACEHOLDER_OWNER}` line in {path.name}; set it with /agent-config",
        )
    try:
        path.write_text(text.replace(placeholder, f"  name: {json.dumps(name)}\n", 1), encoding="utf-8")
    except OSError as exc:
        return ("failed", f"cannot write {path.name}: {exc}")
    return ("done", f"owner is {name}")


def owner_name(root: Path) -> str:
    import yaml

    try:
        data = as_dict(yaml.safe_load(registry_path(root).read_text(encoding="utf-8")))
    except OSError, yaml.YAMLError:
        return ""
    owner = as_dict(data.get("owner"))
    name = str(owner.get("name") or "").strip()
    return "" if name == PLACEHOLDER_OWNER else name


LOCAL = "agent.local.yaml"
LOCAL_REPO_KEYS = ("path", "worktrees_dir")
LOCAL_KEYS = (
    "repos",
    "notes_sync",
    "machine",
    "maintenance",
    "hooks",
    "harness",
    "obsidian_vault",
    "notes_writer",
)
NOTES_SYNC_MODES = ("off", "pull", "on")


def notes_sync_mode(root: Path) -> str:
    import yaml

    try:
        data = as_dict(yaml.safe_load((root / LOCAL).read_text(encoding="utf-8")))
    except OSError, ValueError, yaml.YAMLError:
        return "off"
    value = data.get("notes_sync")
    if isinstance(value, bool):
        return "on" if value else "off"
    return value if isinstance(value, str) and value in NOTES_SYNC_MODES else "off"


def local_section(root: Path, key: str) -> Any:
    import yaml

    try:
        data = yaml.safe_load((root / LOCAL).read_text(encoding="utf-8"))
    except OSError, ValueError, yaml.YAMLError:
        return None
    return as_dict(data).get(key)


def machine_root(root: Path) -> Path:
    from .worktree import main_checkout

    try:
        return main_checkout(root)
    except Exception:
        return root


def machine_section(root: Path, key: str) -> Any:
    return local_section(machine_root(root), key)


def not_relative(value: str) -> bool:
    import re

    v = value.strip()
    return v.startswith(("/", "\\", "~")) or bool(re.match("^[A-Za-z]:[\\\\/]", v))


def load_local(root: Path, shorts: set[str]) -> dict[str, dict[str, str]]:
    import yaml
    from .hooks import toggles

    machine_file = machine_root(root) / LOCAL
    if machine_file != root / LOCAL and _present(machine_file):
        try:
            yaml.safe_load(machine_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AgentError(f"{machine_file} is a broken link; remove it or point it at a file") from exc
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise AgentError(f"{machine_file}: unreadable or invalid YAML: {exc}") from exc
    if found := toggles.problems(machine_section(root, "hooks"), "agent.local.yaml"):
        raise AgentError(f"{machine_file}: " + "; ".join(found))
    path = root / LOCAL
    if not _present(path):
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except FileNotFoundError as exc:
        raise AgentError(f"{path} is a broken link; remove it or point it at a file") from exc
    except yaml.YAMLError as exc:
        raise AgentError(f"{path}: invalid YAML: {exc}") from exc
    content = [line for line in text.splitlines() if line.strip() and (not line.lstrip().startswith("#"))]
    if data is None and (not content):
        return {}
    local = as_dict_or_none(data)
    if local is not None and "peer_net" in local:
        raise AgentError(
            f"{path}: `peer_net:` is no longer read here; cross-device enrollment lives in the harness store (`bin/agent peer-net show`). Remove the block."
        )
    if local is None or set(local) - set(LOCAL_KEYS):
        raise AgentError(
            f"{path}: only {', '.join((f'`{k}:`' for k in LOCAL_KEYS))} are supported; repos is {{<short>: {{path: ...}}}}"
        )
    repos = as_dict_or_none(local.get("repos", {}))
    if repos is None:
        raise AgentError(f"{path}: `repos` must map a repo short to its overrides")
    problems: list[str] = []
    out: dict[str, dict[str, str]] = {}
    for short, raw in repos.items():
        if short not in shorts:
            problems.append(f"repos.{short} is not a repo in agent.yaml")
            continue
        overrides = as_dict_or_none(raw)
        if not overrides:
            problems.append(f"repos.{short} must set path and/or worktrees_dir")
            continue
        for key, value in overrides.items():
            if key not in LOCAL_REPO_KEYS:
                problems.append(f"repos.{short}.{key} cannot be set here; only {', '.join(LOCAL_REPO_KEYS)}")
            elif not isinstance(value, str) or not value.strip():
                problems.append(f"repos.{short}.{key} must be a non-empty path")
            else:
                out.setdefault(short, {})[key] = str(Path(value.strip()).expanduser())
    if problems:
        raise AgentError(f"{path}: " + "; ".join(problems))
    return out


def apply_local(root: Path, repos: list[Any]) -> list[Any]:
    from dataclasses import replace

    local = load_local(root, {r.short for r in repos} | {r.name for r in repos})
    if not local:
        return repos
    both = [r.short for r in repos if r.short != r.name and r.short in local and (r.name in local)]
    if both:
        raise AgentError(
            f"{root / LOCAL}: {', '.join(both)} is set under both its name and its short; keep one block"
        )
    out: list[Any] = []
    for repo in repos:
        overrides = local.get(repo.short) or local.get(repo.name) or {}
        out.append(replace(repo, **overrides, local=tuple(sorted(overrides))) if overrides else repo)
    return out
