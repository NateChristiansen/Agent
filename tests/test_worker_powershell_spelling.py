"""A Codex worker on Windows: its PowerShell spellings of granted POSIX commands are allowed, and
nothing the POSIX spelling would refuse gets through."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from agent_cli.dispatch import codex_permissions, powershell_spelling
from agent_cli.dispatch.powershell_spelling import respelled
from dispatch_helpers import (
    _set_alpha_dispatch as set_alpha_dispatch,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_spelling, "powershell_host", lambda: True)
    # C: is the local disk; every other letter stands in for a mapped share.
    monkeypatch.setattr(powershell_spelling, "drive_is_local", lambda letter: letter.upper() == "C")


#: Each may name a path off this machine, so it gets no twin.
REMOTE = [
    r"Get-Content -LiteralPath:\\host\share\x",  # colon-bound value
    r"Get-Content a.txt,\\host\share\x",  # comma array
    r"Get-Content FileSystem::\\host\share\x",  # provider-qualified
    r"Get-Content Microsoft.PowerShell.Core\FileSystem::C:\x",
    r"Get-Content a.txt, b.txt",  # any comma
    r"Get-Content \\?\C:\x",  # device paths
    r"Get-Content \\.\pipe\x",
    r"Get-Content //host/share/x",
    r"Get-Content '\''\host\share\x'",  # quotes cannot split the prefix
    r"Get-Content Z:\x",  # a mapped drive
    r"Get-Content Z:x",  # drive-relative on a mapped drive
    r"Get-Content -Path:Z:\x",
    r"Get-Content Remote:\x",  # a named drive (New-PSDrive, maybe on a share)
    r"Get-Content -LiteralPath:Remote:\x",
    r"Get-ChildItem HKLM:\Software",  # registry provider
    r"Get-Content Env:PATH",  # environment provider
    r"Get-ChildItem Cert:\CurrentUser",
    r"Get-Content Temp:\x",
]


@pytest.mark.parametrize("body", REMOTE)
def test_a_possibly_remote_operand_gets_no_twin(windows: None, body: str) -> None:
    assert respelled(body) is None


@pytest.mark.parametrize("body", REMOTE)
def test_a_possibly_remote_operand_stays_denied(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, windows: None, body: str
) -> None:
    """Codex's three probes (colon-bound, comma array, provider-qualified) went deny -> allow."""
    assert _decision(tmp_root, monkeypatch, body)[0] == "deny"


def test_a_colon_bound_parameter_gets_no_twin_even_on_a_local_drive(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, windows: None
) -> None:
    """Intentional: `-LiteralPath:` reads
    as a named drive prefix, so even a local `C:` value is judged by the original rules and
    denied. Fail-closed is accepted over parsing PowerShell parameter binding. The plain
    `Get-Content C:/x` spelling keeps its twin."""
    assert respelled("Get-Content -LiteralPath:C:/x") is None
    assert _decision(tmp_root, monkeypatch, "Get-Content -LiteralPath:C:/x")[0] == "deny"
    assert respelled("Get-Content C:/x") == "cat C:/x"


@pytest.mark.skipif(os.name == "nt", reason="off Windows only")
def test_off_windows_no_drive_counts_as_local() -> None:
    assert not powershell_spelling.drive_is_local("C")


@pytest.mark.parametrize(
    ("body", "twin"),
    [
        (r".venv\Scripts\python.exe --version", ".venv/Scripts/python.exe --version"),
        (r"Get-Content src\agent_cli\dispatch\mcp_policy.py", r"cat 'src\agent_cli\dispatch\mcp_policy.py'"),
        ("get-content -TotalCount 20 README.md", "cat -TotalCount 20 README.md"),
        (r'gc "docs\a b.md" | Select-Object -First 5', r"cat 'docs\a b.md' | head -First 5"),
        (r"Select-String -Pattern TODO src\x.py", r"grep -Pattern TODO 'src\x.py'"),
        (r"Get-ChildItem tests\fixtures", r"ls 'tests\fixtures'"),
        ("Get-Location", "pwd"),
        (r"git -C C:\wt\one log -1", r"git -C 'C:\wt\one' log -1"),
    ],
)
def test_a_powershell_body_gets_its_posix_twin(windows: None, body: str, twin: str) -> None:
    assert respelled(body) == twin


@pytest.mark.parametrize(
    "body",
    [
        "cat README.md",  # already its own twin
        "Get-Content $env:USERPROFILE\\.ssh\\id_rsa",  # `$` expands
        "Get-Content (Remove-Item x)",  # a subexpression runs
        "Get-Content x; Remove-Item y",  # a command list
        "Get-Content x || Remove-Item y",  # PowerShell 7 chain
        "Get-Content x > y",  # redirection
        "Get-Content `\nx",  # backtick continuation
        "Get-Content \u2018x\u2019",  # smart quotes are quotes to PowerShell
        r"Get-Content \\server\share\x",  # UNC reaches the network
        "Get-Content 'unclosed",
        "Remove-Item -Recurse x",  # write cmdlets are never mapped
    ],
)
def test_a_body_without_a_safe_twin_is_left_alone(windows: None, body: str) -> None:
    assert respelled(body) is None


def test_off_windows_nothing_is_respelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_spelling, "powershell_host", lambda: False)
    assert respelled(r"Get-Content src\x.py") is None


def _decision(root: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> tuple[str, str]:
    worktree = root / "wt"
    worktree.mkdir(exist_ok=True)
    d: dict[str, Any] = {"id": "999-99", "repo": "alpha", "worktree": str(worktree), "thread": []}
    monkeypatch.setattr(codex_permissions.state, "load", lambda _root, _id: d)
    return codex_permissions._decision(  # pyright: ignore[reportPrivateUsage]
        root, "999-99", {"tool_name": "Bash", "tool_input": {"command": command}}
    )


@pytest.mark.parametrize(
    "command",
    [
        r"Get-Content src\agent_cli\dispatch\mcp_policy.py",
        r".venv\Scripts\python.exe --version",
        r"Get-Content README.md | Select-Object -First 20",
        r"Get-FileHash -Algorithm SHA256 spec.md",
    ],
)
def test_codex_on_windows_allows_the_spelling_of_a_granted_read(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, windows: None, command: str
) -> None:
    assert _decision(tmp_root, monkeypatch, command)[0] == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item -Recurse src",
        "rm -rf src",
        "Get-Content $(Remove-Item x)",
        "Get-Content x; Remove-Item y",
        r"git -C C:\wt push",
    ],
)
def test_codex_on_windows_still_denies_what_posix_denies(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, windows: None, command: str
) -> None:
    assert _decision(tmp_root, monkeypatch, command)[0] == "deny"


def test_a_deny_rule_on_the_twin_wins_over_the_original_spelling(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, windows: None
) -> None:
    set_alpha_dispatch(tmp_root, allowed_tools=["Bash(Get-Content:*)"])
    rules = {"allow": ["Bash(Get-Content:*)", "Bash(cat:*)"], "deny": ["Bash(cat secret*)"]}
    monkeypatch.setattr(codex_permissions, "dispatch_rules", lambda *_a: (rules, []))
    verdict, reason = _decision(tmp_root, monkeypatch, "Get-Content secret.txt")
    assert verdict == "deny" and "cat secret" in reason
    assert _decision(tmp_root, monkeypatch, "Get-Content notes.txt")[0] == "allow"


def test_off_windows_the_powershell_spelling_is_still_unknown(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(powershell_spelling, "powershell_host", lambda: False)
    assert _decision(tmp_root, monkeypatch, r"Get-Content src\x.py")[0] == "deny"
