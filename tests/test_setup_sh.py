"""setup.sh runs under the bash macOS ships, 3.2 (a plain `./setup.sh` there once died with
`args[@]: unbound variable`)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.name == "nt", reason="setup.sh is the macOS and Linux entry point")


@pytest.mark.parametrize("args", [[], ["--yes"]])
def test_setup_sh_hands_over_with_and_without_arguments(tmp_path: Path, args: list[str]) -> None:
    bash = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
    assert bash
    clone = tmp_path / "clone"
    clone.mkdir()
    shutil.copy(ROOT / "setup.sh", clone / "setup.sh")
    fake = tmp_path / "bin"
    fake.mkdir()
    uv = fake / "uv"
    uv.write_text('#!/bin/sh\necho "uv $*"\n', encoding="utf-8")
    uv.chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(
        [bash, str(clone / "setup.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert result.stdout.splitlines()[-1] == " ".join([f"uv run agent --root {clone} setup", *args])


@pytest.mark.parametrize("args", [[], ["--check"]])
def test_setup_sh_targets_its_own_clone_despite_a_foreign_agent_root(tmp_path: Path, args: list[str]) -> None:
    clone = tmp_path / "clone"
    (clone / ".venv").mkdir(parents=True)
    shutil.copy(ROOT / "setup.sh", clone / "setup.sh")
    fake = tmp_path / "bin"
    fake.mkdir()
    uv = fake / "uv"
    uv.write_text('#!/bin/sh\necho "uv $*"\n', encoding="utf-8")
    uv.chmod(0o755)
    env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AGENT_ROOT": str(tmp_path / "other-install"),
    }
    result = subprocess.run(
        ["/bin/bash", str(clone / "setup.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"agent --root {clone} setup" in result.stdout.splitlines()[-1]
