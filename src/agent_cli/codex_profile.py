"""Generate the per-clone Codex manager profile in ignored `.codex/config.toml`. Writable roots
include linked notes and the harness worktree parent so managers can edit their own checkouts.
notes re-exports the public builders.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tomllib
from pathlib import Path

from .core import atomic_write
from .records.narrow import dig

CODEX_CONFIG_FILE = Path(".codex") / "config.toml"
_CODEX_BEGIN = "# BEGIN agent setup: Codex manager profile"
_CODEX_END = "# END agent setup: Codex manager profile"
_OLD_CODEX_BEGIN = "# BEGIN agent setup: notes writable root"
_OLD_CODEX_END = "# END agent setup: notes writable root"


_SSH_URL = re.compile(r"^(?:ssh://)?(?:[^@/\s]+@)?([A-Za-z0-9.-]+)[:/]")
_HTTPS_URL = re.compile(r"^https?://(?:[^@/\s]+@)?([A-Za-z0-9.-]+)[:/]")


def notes_remote_grants(notes_root: Path | None) -> tuple[list[str], list[str]]:
    """Grant the notes remote host and, for SSH remotes, the owner's current agent socket. Login can
    change the socket path, so setup must refresh that grant.
    """
    if notes_root is None:
        return [], []
    try:
        url = subprocess.run(
            ["git", "-C", str(notes_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return [], []
    if found := _HTTPS_URL.match(url):
        return [found.group(1)], []
    if found := _SSH_URL.match(url):
        agent = os.environ.get("SSH_AUTH_SOCK", "")
        return [found.group(1)], [agent] if agent else []
    return [], []


def configure_codex_notes_root(
    root: Path,
    notes_root: Path | str | None = None,
    *,
    codex_sandbox: str | None = None,
    codex_inline: bool | None = None,
) -> tuple[str, str]:
    """Configure a narrow Codex manager profile, with write access to the harness worktree
    parent and, optionally, the linked notes repo.
    A permission profile allows Claude inbox sockets and api.github.com through the proxy, plus
    the notes remote's host and, for an SSH remote, the SSH agent socket (`notes_remote_grants`).
    Automatic review keeps the sandbox boundary. Preserve an explicit reviewer override in
    this ignored, per-clone config.

    Windows trials MXC by default. An explicit elevated rollback and the opt-in inline TUI
    survive subsequent setup runs in the generated block. Other platforms write neither key.
    """
    from . import worktree_grants

    writable: list[Path] = []
    if notes_root is not None:
        target = Path(notes_root).expanduser()
        if not target.is_absolute():
            target = root / target
        writable.append(target.resolve())
    parent = worktree_grants.parent_for(root)
    if parent not in writable:
        writable.append(parent)
    domains, sockets = notes_remote_grants(writable[0] if notes_root is not None else None)
    domains = ["api.github.com", *(d for d in domains if d != "api.github.com")]
    inbox = "/tmp/cc-socks"  # noqa: S108 — Claude Code's own inbox socket directory
    sockets = [inbox, *(s for s in sockets if s != inbox)]
    path = root / CODEX_CONFIG_FILE
    if path.is_symlink():
        return "refused", f"{CODEX_CONFIG_FILE} is a symlink; update it yourself"
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    try:
        tomllib.loads(current)
    except tomllib.TOMLDecodeError as exc:
        return "refused", f"{CODEX_CONFIG_FILE} is invalid TOML: {exc}"

    markers = [
        pair
        for pair in ((_CODEX_BEGIN, _CODEX_END), (_OLD_CODEX_BEGIN, _OLD_CODEX_END))
        if pair[0] in current or pair[1] in current
    ]
    if len(markers) > 1 or any(
        current.count(begin) != 1 or current.count(end) != 1 for begin, end in markers
    ):
        return "refused", f"{CODEX_CONFIG_FILE} has incomplete or duplicate generated markers"
    managed: dict[str, object] = {}
    if markers:
        begin, end = markers[0]
        before, rest = current.split(begin, 1)
        generated, after = rest.split(end, 1)
        managed = tomllib.loads(generated)
        other = (before + after.removeprefix("\n")).strip("\n")
    else:
        other = current.strip("\n")
    try:
        unmanaged = tomllib.loads(other)
    except tomllib.TOMLDecodeError as exc:
        return "refused", f"{CODEX_CONFIG_FILE} conflicts with generated section: {exc}"
    if any(key in unmanaged for key in ("sandbox_mode", "sandbox_workspace_write", "default_permissions")):
        return "refused", f"{CODEX_CONFIG_FILE} already manages Codex sandbox permissions"
    if "network_proxy" in unmanaged.get("features", {}):
        return "refused", f"{CODEX_CONFIG_FILE} already manages the network proxy"
    if "agent-manager" in unmanaged.get("permissions", {}):
        return "refused", f"{CODEX_CONFIG_FILE} already manages the agent-manager profile"

    windows = ""
    sandbox = ""
    if codex_sandbox not in (None, "mxc", "elevated"):
        return "refused", "Codex sandbox must be mxc or elevated"
    if platform.system() == "Windows":
        sandbox = codex_sandbox or ("elevated" if dig(managed, "windows", "sandbox") == "elevated" else "mxc")
        inline = (
            codex_inline if codex_inline is not None else dig(managed, "tui", "alternate_screen") == "never"
        )
        windows = f'[windows]\nsandbox = "{sandbox}"\n'
        if inline:
            windows += '[tui]\nalternate_screen = "never"\n'

    block = (
        f"{_CODEX_BEGIN}\n"
        'default_permissions = "agent-manager"\n'
        + ('approvals_reviewer = "auto_review"\n' if "approvals_reviewer" not in unmanaged else "")
        + windows
        + "[features]\nnetwork_proxy = true\n"
        + '[permissions.agent-manager]\nextends = ":workspace"\n'
        + "[permissions.agent-manager.filesystem]\n"
        + "".join(f'{json.dumps(str(p))} = "write"\n' for p in writable)
        + "[permissions.agent-manager.network]\nenabled = true\n"
        + "[permissions.agent-manager.network.domains]\n"
        + "".join(f'{json.dumps(d)} = "allow"\n' for d in domains)
        + "[permissions.agent-manager.network.unix_sockets]\n"
        + "".join(f'{json.dumps(s)} = "allow"\n' for s in sockets)
        + f"{_CODEX_END}\n"
    )
    # Top-level settings must stay before the generated tables. Existing tables may follow.
    lines = other.splitlines(keepends=True)
    table_starts = ("[", "# BEGIN agent setup: Codex agent MCP")
    first_table = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith(table_starts)), len(lines)
    )
    # No compaction limit is written: Codex's usable window is about 258k, so its default
    # applies. An owner-set limit in the prelude is kept as is.
    prelude = "".join(lines[:first_table]).strip("\n")
    tables = "".join(lines[first_table:]).strip("\n")
    updated = (prelude + "\n\n" if prelude else "") + block + ("\n" + tables + "\n" if tables else "")
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        return "refused", f"{CODEX_CONFIG_FILE} conflicts with generated section: {exc}"
    detail = f"{CODEX_CONFIG_FILE} allows Claude inbox sockets, linked notes and {parent}"
    if windows:
        detail += (
            f'; Windows sandbox = {sandbox}. If the project key does not win, set sandbox = "{sandbox}" '
            "under [windows] in the user config ($CODEX_HOME/config.toml or ~/.codex/config.toml); "
            "restart Codex and check doctor and sandbox logs"
        )
    if updated == current:
        return "already", detail
    atomic_write(path, updated)
    return "done", detail
