"""Read the effective Windows PowerShell policy without changing it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

_QUERY = (
    "$scopes = @(Get-ExecutionPolicy -List | ForEach-Object { "
    "@{ scope = $_.Scope.ToString(); policy = $_.ExecutionPolicy.ToString() } }); "
    "@{ effective = (Get-ExecutionPolicy).ToString(); scopes = $scopes } "
    "| ConvertTo-Json -Compress -Depth 4"
)
_POLICIES = {"Restricted", "AllSigned", "RemoteSigned", "Unrestricted", "Bypass", "Undefined"}
_SCOPES = ("MachinePolicy", "UserPolicy", "Process", "CurrentUser", "LocalMachine")


def query() -> tuple[str | None, dict[str, str], str]:
    """Return (effective, scope map, error) from the unmodified PowerShell session."""
    shell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not shell:
        return None, {}, "PowerShell is not on PATH"
    # `setup.ps1` runs under `-ExecutionPolicy Bypass`, which PowerShell passes to children as
    # PSExecutionPolicyPreference, so this query reported the setup run's Process scope (Bypass)
    # instead of the policy a new shell gets (which may be RemoteSigned).
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSEXECUTIONPOLICYPREFERENCE"}
    try:
        proc = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, {}, f"PowerShell policy query failed: {exc}"
    if proc.returncode != 0:
        return None, {}, f"PowerShell policy query failed (exit {proc.returncode})"
    try:
        data: Any = json.loads(proc.stdout)
        effective = data["effective"]
        rows = data["scopes"]
        scopes = {row["scope"]: row["policy"] for row in rows}
        if effective not in _POLICIES or any(
            scope not in _SCOPES or policy not in _POLICIES for scope, policy in scopes.items()
        ):
            raise ValueError("unexpected policy value")
    except KeyError, TypeError, ValueError:
        return None, {}, "PowerShell policy query returned an unreadable result"
    return effective, scopes, ""


def guidance(effective: str, scopes: dict[str, str], *, cmd_works: bool) -> str:
    """Explain the no-policy-change launcher and the optional user-scoped alternative."""
    locked = any(scopes.get(scope, "Undefined") != "Undefined" for scope in _SCOPES[:2])
    shim = "codex.cmd works" if cmd_works else "check that npm installed codex.cmd"
    if effective in ("Restricted", "AllSigned"):
        detail = f"PowerShell policy {effective}; npm's codex.ps1 may be blocked; {shim}"
    else:
        detail = f"PowerShell policy {effective}; {shim} bypasses the script launcher"
    if locked:
        return detail + "; Group Policy takes precedence over CurrentUser settings"
    return detail + "; optional: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"


def codex_launch_ok(effective: str | None, *, cmd_works: bool) -> bool:
    """A restrictive script policy needs a working command shim for Codex launch."""
    return effective is not None and (effective not in ("Restricted", "AllSigned") or cmd_works)


def cmd_shim_works() -> bool:
    """Exercise npm's command shim without starting a session. This can start Node for 1-2 s."""
    shim = shutil.which("codex.cmd")
    if not shim:
        return False
    try:
        return (
            subprocess.run(
                [shim, "--version"], capture_output=True, text=True, timeout=10, check=False
            ).returncode
            == 0
        )
    except OSError, subprocess.SubprocessError:
        return False
