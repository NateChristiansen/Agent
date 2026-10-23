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
from collections.abc import Iterator
from pathlib import Path

import pytest

import suite_cost
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


@pytest.fixture(autouse=True, scope="session")
def _host_facts_uncounted() -> Iterator[None]:
    """What the host and the running harness cost to ask, kept out of each test's spawn figure.

    The client-ancestor walk (`session_identity.client_process`) starts at this process's parent
    and climbs the host's process tree with a `ps` per step, so its cost is how pytest was launched:
    94 call-phase starts for one worktree test under a landing, 70 alone, 118 under a probe record.
    Those steps leave the running test's figure; a walk over a pid a test made up still counts.
    `harness_revision` is cached for the process, so it is asked here, before any test pays for it."""
    from agent_cli import session_identity
    from agent_cli.dispatch import telemetry_events

    command, parent = session_identity._command, session_identity._parent_pid
    host: set[int] = set()
    with suite_cost.uncounted():
        telemetry_events.harness_revision()
        pid: int | None = os.getppid()
        while pid and pid > 1 and pid not in host:
            host.add(pid)
            pid = parent(pid)

    def host_command(pid: int) -> str:
        if pid not in host:
            return command(pid)
        with suite_cost.uncounted():
            return command(pid)

    def host_parent(pid: int) -> int | None:
        if pid not in host:
            return parent(pid)
        with suite_cost.uncounted():
            return parent(pid)

    session_identity._command, session_identity._parent_pid = host_command, host_parent
    yield
    session_identity._command, session_identity._parent_pid = command, parent


@pytest.fixture(autouse=True)
def _no_fence_violations(_live_state_fence: Path):
    path = _live_state_fence / fence.VIOLATIONS
    before = path.stat().st_size if path.exists() else 0
    yield
    if path.exists() and path.stat().st_size > before:
        with open(path, encoding="utf-8") as fh:
            fh.seek(before)
            pytest.fail(f"telemetry tried to record outside the test temp root:\n{fh.read()}")
