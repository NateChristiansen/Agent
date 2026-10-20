"""The effective Codex shell policy shared by preview, preflight and transport.

Callers may pass filesystem permissions and a native Codex permissions profile. Their
read restrictions are descriptive here: workspaceWrite has no readOnlyRoots field.
Codex enforces them only through the named profile in its own config.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import repos
from ..codex_executable import command
from ..core import AgentError


@dataclass(frozen=True)
class CodexPolicy:
    command: tuple[str, ...]
    executable_error: str | None
    writable_roots: tuple[str, ...]
    network_access: bool
    approval_policy: str
    filesystem_permissions: tuple[tuple[str, str], ...] = ()
    permissions_profile: str | None = None

    def server_command(self, *overrides: str) -> tuple[str, ...]:
        """Resolve once, then append only trusted app-server config overrides."""
        if self.executable_error:
            raise AgentError(self.executable_error)
        return (*self.command, *overrides)

    def thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"approvalPolicy": self.approval_policy}
        if self.permissions_profile is None:
            params["sandbox"] = "workspace-write"
        return params

    def turn_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"approvalPolicy": self.approval_policy}
        if self.permissions_profile is None:
            params["sandboxPolicy"] = self.sandbox_policy()
        return params

    def sandbox_policy(self) -> dict[str, Any]:
        return {
            "type": "workspaceWrite",
            "writableRoots": list(self.writable_roots),
            "networkAccess": self.network_access,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }

    def preview(self) -> dict[str, Any]:
        return {
            "transport": "app-server --stdio",
            "command": list(self.command),
            "cli_path": self.command[0] if self.command else None,
            "executable_error": self.executable_error,
            "approval_policy": self.approval_policy,
            "sandbox": "permissions-profile" if self.permissions_profile else "workspaceWrite",
            "sandbox_policy": None if self.permissions_profile else self.sandbox_policy(),
            "filesystem_permissions": dict(self.filesystem_permissions),
            "permissions_profile": self.permissions_profile,
        }


def writable_roots(worktree: Path, *, clone: Path | None = None) -> list[str]:
    """The current legacy worker roots, including the pinned repo's Git metadata.

    A dry run has no worktree yet; it can inspect the existing clone without creating
    refs or directories. A named permissions profile replaces these roots. A root that is
    not strictly valid UTF-8 refuses: a replaced or merged path must never be writable.
    """
    roots = _roots(worktree, clone)
    for root in roots:
        try:
            root.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AgentError(f"writable root is not valid UTF-8 ({exc.reason}): {root!a}") from None
    return roots


def _roots(worktree: Path, clone: Path | None) -> list[str]:
    target = worktree if worktree.is_dir() else clone
    if target is None:
        return [str(worktree)]
    proc = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if proc.returncode != 0:
        raise AgentError(f"cannot locate worktree Git metadata: {proc.stderr.strip()}")
    common = Path(proc.stdout.strip()).resolve()
    if not common.is_dir() or common.name != ".git":
        raise AgentError(f"unexpected worktree Git metadata path: {common}")
    return [str(worktree), str(common)]


def policy_for(
    d: dict[str, Any],
    repo: repos.Repo,
    *,
    root: Path | None = None,
    writable: list[str] | tuple[str, ...] | None = None,
    filesystem_permissions: tuple[tuple[str, str], ...] = (),
    permissions_profile: str | None = None,
    approval_policy: str = "on-request",
    config_overrides: tuple[str, ...] = (),
) -> CodexPolicy:
    """Construct the same policy for a planned launch and its running attempt.

    Missing executables are visible in previews, and refuse when server_command is
    used. This constructor performs no write, network call or permission grant.
    """
    error = None
    try:
        argv = tuple(command("app-server", "--stdio", *config_overrides))
    except FileNotFoundError as exc:
        argv, error = (), str(exc)
    if writable is None:
        worktree = Path(str(d.get("worktree") or "."))
        clone = repo.abs_path(root) if root is not None else None
        writable = writable_roots(worktree, clone=clone) if d.get("worktree") else []
    return CodexPolicy(
        argv,
        error,
        tuple(writable),
        repos.dispatch_bool(repo.dispatch, "codex_network_access"),
        approval_policy,
        filesystem_permissions,
        permissions_profile,
    )
