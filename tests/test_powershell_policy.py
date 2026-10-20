"""Windows shell policy is diagnosed without changing a policy or requiring a PS script."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_cli import powershell_policy, setup


@pytest.mark.parametrize(
    ("effective", "machine", "fragment"),
    [
        ("Restricted", "Undefined", "codex.ps1 may be blocked"),
        ("RemoteSigned", "Undefined", "codex.cmd works"),
        ("Restricted", "Restricted", "Group Policy takes precedence"),
    ],
)
def test_effective_policy_and_group_override(
    monkeypatch: pytest.MonkeyPatch, effective: str, machine: str, fragment: str
):
    calls: list[list[str]] = []
    monkeypatch.setattr(powershell_policy.shutil, "which", lambda name: "C:/Windows/powershell.exe")

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "effective": effective,
                    "scopes": [
                        {"scope": "MachinePolicy", "policy": machine},
                        {"scope": "CurrentUser", "policy": "RemoteSigned"},
                    ],
                }
            ),
        )

    monkeypatch.setattr(powershell_policy.subprocess, "run", run)
    policy, scopes, error = powershell_policy.query()
    assert error == "" and policy == effective
    assert policy is not None
    assert fragment in powershell_policy.guidance(policy, scopes, cmd_works=True)
    assert powershell_policy.codex_launch_ok(policy, cmd_works=True)
    assert len(calls) == 1
    assert calls[0][:4] == ["C:/Windows/powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert all("Bypass" not in arg and "Set-ExecutionPolicy" not in arg for arg in calls[0])


def test_the_setup_runs_bypass_does_not_reach_the_query(monkeypatch: pytest.MonkeyPatch):
    """setup.ps1 runs under -ExecutionPolicy Bypass, which a child PowerShell inherits through
    PSExecutionPolicyPreference; a new shell's RemoteSigned policy could read as Bypass."""
    monkeypatch.setenv("PSExecutionPolicyPreference", "Bypass")
    monkeypatch.setattr(powershell_policy.shutil, "which", lambda name: "C:/Windows/powershell.exe")
    seen: list[dict[str, str]] = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"effective": "RemoteSigned", "scopes": []}))

    monkeypatch.setattr(powershell_policy.subprocess, "run", run)
    powershell_policy.query()
    assert not any(key.upper() == "PSEXECUTIONPOLICYPREFERENCE" for key in seen[0])


def test_cmd_shim_is_exercised_without_starting_a_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(powershell_policy.shutil, "which", lambda name: "C:/npm/codex.cmd")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(powershell_policy.subprocess, "run", run)
    assert powershell_policy.cmd_shim_works()
    assert calls == [["C:/npm/codex.cmd", "--version"]]


def test_setup_reports_an_unreadable_policy_without_failing_a_requirement(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(setup.shutil, "which", lambda name: "C:/tool.exe")
    monkeypatch.setattr("agent_cli.installs.refresh_windows_path", lambda: None)
    monkeypatch.setattr("agent_cli.installs.obsidian_app_found", lambda system: "C:/Obsidian.exe")
    monkeypatch.setattr(
        "agent_cli.obsidian.cli_row", lambda name, guide: setup._row(name, True, "C:/obsidian.exe", "")
    )
    monkeypatch.setattr(powershell_policy, "query", lambda: (None, {}, "policy query timed out"))
    rows = setup.checks()
    policy = next(row for row in rows if row["name"] == "PowerShell policy")
    assert policy["found"] == "unverified"
    assert policy["guide"] == "policy query timed out"
    assert "PowerShell policy" not in setup.missing_requirements(rows)


@pytest.mark.parametrize(
    ("effective", "shim_works", "expected"),
    [
        ("Restricted", False, False),
        ("AllSigned", False, False),
        ("Restricted", True, True),
        ("RemoteSigned", False, True),
    ],
)
def test_setup_flags_a_blocked_codex_launcher(
    monkeypatch: pytest.MonkeyPatch, effective: str, shim_works: bool, expected: bool
):
    monkeypatch.setattr(setup.shutil, "which", lambda name: "C:/tool.exe")
    monkeypatch.setattr("agent_cli.installs.refresh_windows_path", lambda: None)
    monkeypatch.setattr("agent_cli.installs.obsidian_app_found", lambda system: "C:/Obsidian.exe")
    monkeypatch.setattr(
        "agent_cli.obsidian.cli_row", lambda name, guide: setup._row(name, True, "C:/obsidian.exe", "")
    )
    monkeypatch.setattr(powershell_policy, "query", lambda: (effective, {}, ""))
    monkeypatch.setattr(powershell_policy, "cmd_shim_works", lambda: shim_works)
    policy = next(row for row in setup.checks("Windows") if row["name"] == "PowerShell policy")
    assert policy["ok"] is expected
    assert powershell_policy.codex_launch_ok(effective, cmd_works=shim_works) is expected
