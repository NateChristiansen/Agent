"""setup.ps1 and bin/agent.ps1 must not let a caller's AGENT_ROOT send setup to another install.

Static checks of the script text run everywhere; the live check needs a PowerShell and skips
without one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def test_setup_ps1_passes_its_own_directory_on_every_agent_setup_call() -> None:
    calls = re.findall(r"^\s*uv run .*\bagent\b.*$", (ROOT / "setup.ps1").read_text(encoding="utf-8"), re.M)
    assert len(calls) == 2
    for call in calls:
        assert "agent --root $PSScriptRoot setup" in call


def test_agent_ps1_restores_the_callers_agent_root() -> None:
    text = (ROOT / "bin" / "agent.ps1").read_text(encoding="utf-8")
    assert text.index("$before = $env:AGENT_ROOT") < text.index("$env:AGENT_ROOT = $root")
    finally_block = text.split("finally", 1)[1]
    assert "$env:AGENT_ROOT = $before" in finally_block
    assert "exit $code" in finally_block.split("}", 1)[1]


@pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell on this machine")
@pytest.mark.parametrize("before", [None, "C:\\other"])
def test_agent_ps1_leaves_agent_root_as_it_found_it(tmp_path: Path, before: str | None) -> None:
    clone = tmp_path / "clone"
    (clone / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "agent.ps1", clone / "bin" / "agent.ps1")
    script = clone / "bin" / "agent.ps1"
    setup = (
        f"$env:AGENT_ROOT = '{before}'"
        if before
        else "Remove-Item Env:AGENT_ROOT -ErrorAction SilentlyContinue"
    )
    probe = f"{setup}; & '{script}' --version; Write-Output \"after=$env:AGENT_ROOT\""
    assert POWERSHELL
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.stdout.splitlines()[-1] == f"after={before or ''}"
