"""Live-state fences for the whole suite.

A fixture harness is not a git repository, so code that asks git for the main checkout used to
walk up from a fixture into whatever checkout enclosed the pytest temp root and record there: the
live harness took 3,624 fixture telemetry events that way (2026-10-05). `GIT_CEILING_DIRECTORIES`
stops that walk at the temp root, for this process and every child it starts.
`AGENT_TEST_SINK_FENCE` makes the recorder refuse a sink outside the temp root and note the
refusal, and `_no_fence_violations` fails the test that caused one. `AGENT_NO_USAGE` keeps every
trigger, here and in each child, from starting a detached usage job: a test that cleared the
fixed clock left two such jobs alive after the run on Windows, holding venv DLLs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli.telemetry import fence
from agent_cli.usage import job as usage_job


@pytest.fixture(autouse=True, scope="session")
def _live_state_fence(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.getbasetemp().resolve()
    os.environ["GIT_CEILING_DIRECTORIES"] = str(base)
    os.environ[fence.FENCE_ENV] = str(base)
    os.environ[usage_job.OFF_ENV] = "1"
    # doctor reads `$CODEX_HOME/config.toml` (else `~/.codex`); the owner's own Codex trust state
    # must not decide a test. Tests that need a Codex home set their own.
    codex_home = base / "codex-home"
    codex_home.mkdir(exist_ok=True)
    os.environ["CODEX_HOME"] = str(codex_home)
    # The owner's running Obsidian must not decide a test either: `obsidian_run.running` asks this
    # socket first, and a missing one falls back to `obsidian.app_running`, which tests stub.
    os.environ["AGENT_OBSIDIAN_CLI_SOCKET"] = str(base / "no-obsidian-cli.sock")
    return base


@pytest.fixture(autouse=True)
def _no_fence_violations(_live_state_fence: Path):
    path = _live_state_fence / fence.VIOLATIONS
    before = path.stat().st_size if path.exists() else 0
    yield
    if path.exists() and path.stat().st_size > before:
        with open(path, encoding="utf-8") as fh:
            fh.seek(before)
            pytest.fail(f"telemetry tried to record outside the test temp root:\n{fh.read()}")
