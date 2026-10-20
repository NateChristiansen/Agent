# ruff: noqa
from __future__ import annotations
import json
import os
import shutil
import subprocess
from typing import Any

_QUERY = "$scopes = @(Get-ExecutionPolicy -List | ForEach-Object { @{ scope = $_.Scope.ToString(); policy = $_.ExecutionPolicy.ToString() } }); @{ effective = (Get-ExecutionPolicy).ToString(); scopes = $scopes } | ConvertTo-Json -Compress -Depth 4"
_POLICIES = {"Restricted", "AllSigned", "RemoteSigned", "Unrestricted", "Bypass", "Undefined"}
_SCOPES = ("MachinePolicy", "UserPolicy", "Process", "CurrentUser", "LocalMachine")


def query() -> tuple[str | None, dict[str, str], str]:
    shell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not shell:
        return (None, {}, "PowerShell is not on PATH")
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
        return (None, {}, f"PowerShell policy query failed: {exc}")
    if proc.returncode != 0:
        return (None, {}, f"PowerShell policy query failed (exit {proc.returncode})")
    try:
        data: Any = json.loads(proc.stdout)
        effective = data["effective"]
        rows = data["scopes"]
        scopes = {row["scope"]: row["policy"] for row in rows}
        if effective not in _POLICIES or any(
            (scope not in _SCOPES or policy not in _POLICIES for scope, policy in scopes.items())
        ):
            raise ValueError("unexpected policy value")
    except KeyError, TypeError, ValueError:
        return (None, {}, "PowerShell policy query returned an unreadable result")
    return (effective, scopes, "")


def guidance(effective: str, scopes: dict[str, str], *, cmd_works: bool) -> str:
    locked = any((scopes.get(scope, "Undefined") != "Undefined" for scope in _SCOPES[:2]))
    shim = "codex.cmd works" if cmd_works else "check that npm installed codex.cmd"
    if effective in ("Restricted", "AllSigned"):
        detail = f"PowerShell policy {effective}; npm's codex.ps1 may be blocked; {shim}"
    else:
        detail = f"PowerShell policy {effective}; {shim} bypasses the script launcher"
    if locked:
        return detail + "; Group Policy takes precedence over CurrentUser settings"
    return detail + "; optional: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"


def codex_launch_ok(effective: str | None, *, cmd_works: bool) -> bool:
    return effective is not None and (effective not in ("Restricted", "AllSigned") or cmd_works)


def cmd_shim_works() -> bool:
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
