"""Load shared `agent.yaml` and machine-local overrides (see ADR-0030).

The notes-owned, gitignored link holds owner identity, forge settings, worker MCP profiles and the
repository list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import AgentError
from .records.narrow import as_dict, as_dict_or_none

REGISTRY = "agent.yaml"


def _present(path: Path) -> bool:
    """A file, or a link to one. A dangling link counts: a broken file must fail, not read as
    absent."""
    return path.exists() or path.is_symlink()


def registry_path(root: Path) -> Path:
    """The config file this clone reads."""
    return root / REGISTRY


PLACEHOLDER_OWNER = "Your Name"
"""Treat the example owner.name as unset so setup and session-start resolve identity consistently."""

EMPTY_REPOS = (
    "# Your repos. agent.example.yaml shows every field; `/agent-config repo` adds one.\nrepos: []\n"
)


def drop_example_repos(path: Path) -> None:
    """A new agent.yaml starts with no repos. The example's `example-service` entry pointed at a
    repo nobody has, and doctor warned about it on every fresh clone.
    `repos:` is the example's last top-level key; the tracked example keeps the entry as the
    reference."""
    text = path.read_text(encoding="utf-8")
    at = text.find("\nrepos:\n")
    if at >= 0:
        path.write_text(text[: at + 1] + EMPTY_REPOS, encoding="utf-8", newline="\n")


def set_owner_name(root: Path, name: str) -> tuple[str, str]:
    """Write `owner.name` over the example's placeholder: `(state, detail)` as a setup step.

    Setup listed "put your name in agent.yaml" as step 1 on every fresh clone instead of asking.
    Only the placeholder line is replaced, so the file keeps its
    comments, and a name already set is never overwritten."""
    import json

    name = " ".join(name.split())
    if not name:
        return "skipped", "no name given"
    if owner_name(root):
        return "already", f"owner is {owner_name(root)}"
    path = registry_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "failed", f"cannot read {path.name}: {exc}"
    placeholder = f"  name: {PLACEHOLDER_OWNER}\n"
    if placeholder not in text:
        return (
            "skipped",
            f"no `owner.name: {PLACEHOLDER_OWNER}` line in {path.name}; set it with /agent-config",
        )
    # JSON string quoting is valid YAML and survives a name with a colon or quote in it.
    try:
        path.write_text(text.replace(placeholder, f"  name: {json.dumps(name)}\n", 1), encoding="utf-8")
    except OSError as exc:  # a read-only or disconnected notes link
        return "failed", f"cannot write {path.name}: {exc}"
    return "done", f"owner is {name}"


def owner_name(root: Path) -> str:
    """Read owner.name, returning empty for an unset or placeholder name. repos.owner_name re-exports
    this helper. Session-start resolves the name once; durable harness prose refers to the owner.
    Never raise from hooks; doctor reports malformed configuration.
    """
    import yaml

    try:
        data = as_dict(yaml.safe_load(registry_path(root).read_text(encoding="utf-8")))
    except OSError, yaml.YAMLError:
        return ""
    owner = as_dict(data.get("owner"))
    name = str(owner.get("name") or "").strip()
    return "" if name == PLACEHOLDER_OWNER else name


EXPORT_IDENTITY_SHAPE = (
    "'export' must be a mapping whose identity has exactly a name and an email, "
    "each a plain non-empty string; the email holds an @"
)


def export_problem(block: object) -> str | None:
    """Why a shared `export:` block is malformed, or None. Its only key is `identity`. A `<`,
    `>` or line break would corrupt the commit's author line, so they are refused."""
    export = as_dict_or_none(block)
    if export is None or set(export) - {"identity"}:
        return EXPORT_IDENTITY_SHAPE
    identity = as_dict_or_none(export.get("identity"))
    if identity is None or set(identity) != {"name", "email"}:
        return EXPORT_IDENTITY_SHAPE
    for value in identity.values():
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "<>\n\r\0"):
            return EXPORT_IDENTITY_SHAPE
    if "@" not in str(identity["email"]):
        return EXPORT_IDENTITY_SHAPE
    return None


def export_identity(root: Path) -> tuple[str, str]:
    """`(name, email)` from `export.identity` in the shared agent.yaml. Absent or malformed
    raises: the public export is never authored with the checkout's git config."""
    import yaml

    path = registry_path(root)
    try:
        data = as_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AgentError(
            f"export needs `export.identity` in agent.yaml, which cannot be read: {exc}"
        ) from exc
    block = data.get("export")
    if block is None:
        raise AgentError(
            "export needs `export.identity` (name and email) in agent.yaml to author the public commit"
        )
    if problem := export_problem(block):
        raise AgentError(f"agent.yaml `export.identity` is malformed: {problem}")
    identity = as_dict(as_dict(block)["identity"])
    return str(identity["name"]).strip(), str(identity["email"]).strip()


LOCAL = "agent.local.yaml"
LOCAL_REPO_KEYS = ("path", "worktrees_dir")
# Top-level keys `agent.local.yaml` may hold; each reader validates its own section.
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
    """This machine's notes sync switch, `notes_sync:` in `agent.local.yaml` (see ADR-0034). Per
    machine on purpose: a key in the shared agent.yaml would be carried to the other machine by
    the sync itself. Absent, unreadable or unknown all mean `off`; a hook must never fail on it."""
    import yaml

    try:
        data = as_dict(yaml.safe_load((root / LOCAL).read_text(encoding="utf-8")))
    except OSError, ValueError, yaml.YAMLError:  # ValueError: undecodable bytes
        return "off"
    value = data.get("notes_sync")
    if isinstance(value, bool):  # YAML 1.1 reads a bare `on` and `off` as booleans
        return "on" if value else "off"
    return value if isinstance(value, str) and value in NOTES_SYNC_MODES else "off"


def local_section(root: Path, key: str) -> Any:
    """One top-level value from `agent.local.yaml`, or None when the file or key is absent.
    An unreadable file reads as absent here; `load_local`, which every registry load runs,
    is what refuses a broken one."""
    import yaml

    try:
        data = yaml.safe_load((root / LOCAL).read_text(encoding="utf-8"))
    except OSError, ValueError, yaml.YAMLError:
        return None
    return as_dict(data).get(key)


def machine_root(root: Path) -> Path:
    """The checkout whose `agent.local.yaml` holds machine-wide switches (maintenance, hook
    toggles, the harness role): the main checkout, so every linked worktree reads the same."""
    from .worktree import main_checkout

    try:
        return main_checkout(root)
    except Exception:  # noqa: BLE001 — a root that is not a git checkout reads its own file
        return root


def machine_section(root: Path, key: str) -> Any:
    """`local_section` of `machine_root`."""
    return local_section(machine_root(root), key)


def not_relative(value: str) -> bool:
    """An absolute path on any OS (`/x`, `C:\\x`, `C:/x`, `\\\\server\\share`) or a `~` path.
    The shared agent.yaml refuses these on every OS, because the notes repo can be shared by
    machines that do not agree on them."""
    import re

    v = value.strip()
    return v.startswith(("/", "\\", "~")) or bool(re.match(r"^[A-Za-z]:[\\/]", v))


def load_local(root: Path, shorts: set[str]) -> dict[str, dict[str, str]]:
    """This machine's overrides from `agent.local.yaml`, or `{}` when there is none.

    The file sits in the clone root. It is gitignored and never linked into the notes repo, and
    it holds only what differs per machine. For now that is each repo's `path` and
    `worktrees_dir`, where an absolute path or a leading `~` is allowed (repo paths "are already not cross-machine"). A relative path resolves from the clone root,
    as in agent.yaml. A key left out inherits from agent.yaml. Anything unexpected fails, and
    the file is never skipped quietly."""
    import yaml

    from .hooks import toggles

    # The hook switches the hooks actually read: this machine's main checkout's file, which a
    # worktree's own file (or its absence) does not change. A bad block there voids every switch,
    # so it fails here (a worktree validated the wrong file).
    machine_file = machine_root(root) / LOCAL
    if machine_file != root / LOCAL and _present(machine_file):
        # Read strictly here: `local_section` treats an unreadable file as absent, which would let
        # a broken main-checkout file void every switch a worktree's hooks read .
        try:
            yaml.safe_load(machine_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AgentError(f"{machine_file} is a broken link; remove it or point it at a file") from exc
        except (OSError, ValueError, yaml.YAMLError) as exc:  # ValueError: invalid UTF-8
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
    content = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if data is None and not content:  # empty or comments only: nothing overridden
        return {}
    local = as_dict_or_none(data)
    if local is not None and "peer_net" in local:
        raise AgentError(
            f"{path}: `peer_net:` is no longer read here; cross-device enrollment lives in the "
            "harness store (`bin/agent peer-net show`). Remove the block."
        )
    # `machine:` is this machine's dispatch label (see ADR-0036); dispatch/machine.py reads and checks
    # it.
    if local is None or set(local) - set(LOCAL_KEYS):
        raise AgentError(
            f"{path}: only {', '.join(f'`{k}:`' for k in LOCAL_KEYS)} are supported; "
            "repos is {<short>: {path: ...}}"
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
    """`repos` with this machine's `agent.local.yaml` overrides applied. Each overridden repo
    records which keys came from the local file in `local`, so doctor can say so."""
    from dataclasses import replace

    local = load_local(root, {r.short for r in repos} | {r.name for r in repos})
    if not local:
        return repos
    both = [r.short for r in repos if r.short != r.name and r.short in local and r.name in local]
    if both:
        raise AgentError(
            f"{root / LOCAL}: {', '.join(both)} is set under both its name and its short; keep one block"
        )
    out: list[Any] = []
    for repo in repos:
        overrides = local.get(repo.short) or local.get(repo.name) or {}
        out.append(replace(repo, **overrides, local=tuple(sorted(overrides))) if overrides else repo)
    return out
