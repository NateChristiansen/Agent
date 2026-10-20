"""The suite tests the checkout it runs in, or refuses to start (phase B).

On 2026-10-07 Codex workers' pytest imported the manager's `src` through an inherited
PYTHONPATH, so reviews passed or failed against main instead of the candidate.
`check` runs once per session from `conftest.pytest_configure`: the imported `agent_cli`, in
this process and in a child interpreter started the way tests start them, must be this
checkout's `src/agent_cli`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import agent_cli

PROBE = "import agent_cli, sys; sys.stdout.write(agent_cli.__file__)"


def expected(rootpath: Path) -> Path:
    return (rootpath / "src" / "agent_cli").resolve()


def imported() -> Path:
    return Path(agent_cli.__file__).resolve().parent


def child(env: dict[str, str] | None = None) -> Path:
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env=os.environ.copy() if env is None else env,
        check=True,
        timeout=30,
    ).stdout
    return Path(out).resolve().parent


def refuse(where: str, found: Path, want: Path) -> None:
    raise pytest.UsageError(
        f"{where} imports agent_cli from {found}, not this checkout's {want}. Tests would check "
        "another tree's code. Run them with this checkout's environment (`uv run pytest` here) "
        "and without a PYTHONPATH that points elsewhere."
    )


def check(rootpath: Path, *, with_child: bool = True) -> None:
    want = expected(rootpath)
    if imported() != want:
        refuse("pytest", imported(), want)
    if with_child and (found := child()) != want:
        refuse("a child interpreter", found, want)
