"""Pinned adapter contracts, with synthetic local processes only."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from agent_cli import core
from agent_cli.core import AgentError
from agent_cli.dispatch import isolated_transport as adapter
from agent_cli.dispatch.options import options_preview, worker_env
from agent_cli.repos import Repo
from fixtures.claude_environment_stub import captures, write_stub

posix_process_groups = pytest.mark.skipif(
    sys.platform == "win32",
    reason="process groups (os.killpg, os.getpgrp) are POSIX-only; Windows reaps through a Job",
)


def test_sdk_drift_fails_closed_before_spawn(tmp_path, monkeypatch):
    script, capture = write_stub(tmp_path)
    monkeypatch.setattr(adapter, "SDK_VERSION", "999.0.0")
    with pytest.raises(AgentError, match="No ambient-environment fallback"):
        adapter.IsolatedClaudeTransport(ClaudeAgentOptions(cli_path=str(script)))
    assert not capture.exists()


def test_sdk_shape_drift_fails_closed(monkeypatch):
    monkeypatch.setattr(adapter.sdk_transport, "_ACTIVE_CHILDREN", None)
    with pytest.raises(AgentError, match="compatibility gate"):
        adapter.check_compatibility()


@pytest.mark.parametrize("name", ["CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"])
def test_explicit_manager_identity_is_rejected(name):
    with pytest.raises(AgentError, match="reserved manager identity"):
        adapter.child_environment(ClaudeAgentOptions(env={name: "fixture"}))


def test_sdk_tags_and_working_directory_are_canonical(tmp_path):
    options = ClaudeAgentOptions(
        cwd=str(tmp_path),
        enable_file_checkpointing=True,
        env={
            "EXPLICIT_FIXTURE": "allowed",
            "CLAUDE_CODE_ENTRYPOINT": "forged",
            "CLAUDE_AGENT_SDK_VERSION": "forged",
            "PWD": "forged",
        },
    )
    env = adapter.child_environment(options)
    assert env == {
        "EXPLICIT_FIXTURE": "allowed",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": adapter.AUDITED_SDK_VERSION,
        "PWD": str(tmp_path),
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "true",
    }
    assert options.env["PWD"] == "forged"


def test_repository_cannot_override_dispatch_ownership(tmp_path):
    repo = Repo(
        name="fixture",
        short="fixture",
        path=".",
        remote="",
        dispatch={
            "env": {
                "AGENT_ROOT": "forged",
                "AGENT_DISPATCH_ID": "forged",
                "AGENT_FEATURE": "forged",
                "EXPLICIT_FIXTURE": "allowed",
            }
        },
    )
    env = worker_env(tmp_path, {"id": "001-01", "feature": 1}, repo)
    assert env["AGENT_ROOT"] == str(tmp_path)
    assert env["AGENT_DISPATCH_ID"] == "001-01"
    assert env["AGENT_FEATURE"] == "001"
    assert env["EXPLICIT_FIXTURE"] == "allowed"


@pytest.mark.parametrize("mode, expected", [("version-invalid", "unrecognized"), ("version-old", "older")])
def test_invalid_cli_version_prevents_main_spawn(tmp_path, mode, expected):
    script, capture = write_stub(tmp_path, mode=mode)
    transport = adapter.IsolatedClaudeTransport(
        ClaudeAgentOptions(cli_path=str(script), env={"PATH": os.environ["PATH"]})
    )
    with pytest.raises(AgentError, match=expected):
        asyncio.run(transport.connect())
    assert [row["kind"] for row in captures(capture)] == ["version"]


def test_version_probe_timeout_reaps_process(tmp_path, monkeypatch):
    assert adapter.VERSION_PROBE_TIMEOUT_SECONDS > 2
    monkeypatch.setattr(adapter, "VERSION_PROBE_TIMEOUT_SECONDS", 2)
    script, capture = write_stub(tmp_path, mode="version-hold")
    transport = adapter.IsolatedClaudeTransport(
        ClaudeAgentOptions(cli_path=str(script), env={"PATH": os.environ["PATH"]})
    )
    with pytest.raises(AgentError, match="Claude CLI version probe timed out after 2s"):
        asyncio.run(transport.connect())
    pid = captures(capture)[0]["pid"]
    # core.is_alive, not os.kill(pid, 0): that is CTRL_C_EVENT on Windows.
    assert not core.is_alive(pid)


def test_cancellation_during_version_probe_reaps_process(tmp_path):
    script, capture = write_stub(tmp_path, mode="version-hold")
    transport = adapter.IsolatedClaudeTransport(
        ClaudeAgentOptions(cli_path=str(script), env={"PATH": os.environ["PATH"]})
    )

    async def cancel():
        task = asyncio.create_task(transport.connect())
        for _ in range(100):
            if capture.exists():
                break
            await asyncio.sleep(0.01)
        assert capture.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(cancel())
    assert not core.is_alive(captures(capture)[0]["pid"])


def test_preview_lists_effective_grant_names_without_values(tmp_path):
    options = ClaudeAgentOptions(cwd=str(tmp_path), env={"EXPLICIT_FIXTURE": "private-fixture-value"})
    preview = options_preview(options)
    assert preview["env"] == sorted(adapter.child_environment(options))
    assert "private-fixture-value" not in repr(preview)
    assert "CLAUDE_CODE_ENTRYPOINT" in preview["env"]
    assert "PWD" in preview["env"]


@posix_process_groups
@pytest.mark.parametrize("exited, live_member", [(True, False), (True, True), (False, True)])
def test_group_permission_error_requires_no_live_members(monkeypatch, exited, live_member):
    from typing import cast

    from anyio.abc import Process

    class FixtureProcess:
        pid = 999999999
        returncode = None

        async def wait(self):
            if exited:
                self.returncode = 0
            return self.returncode

    def denied(_pid, _signal):
        raise PermissionError("fixture process-group race")

    async def no_live_members(_pgid):
        return not live_member

    monkeypatch.setattr(adapter, "_group_has_no_live_members", no_live_members)
    monkeypatch.setattr(adapter.os, "killpg", denied)
    process = cast(Process, FixtureProcess())
    if exited and not live_member:
        asyncio.run(adapter._reap(process))
    else:
        with pytest.raises(PermissionError, match="fixture"):
            asyncio.run(adapter._reap(process))


@posix_process_groups
def test_real_group_observation_distinguishes_live_and_absent_groups():
    assert asyncio.run(adapter._group_has_no_live_members(os.getpgrp())) is False
    assert asyncio.run(adapter._group_has_no_live_members(999999999)) is True


@posix_process_groups
def test_successful_group_signal_without_confirmed_exit_is_failure(monkeypatch):
    from typing import cast

    from anyio.abc import Process

    class FixtureProcess:
        pid = 999999999
        returncode = None

        async def wait(self):
            return None

    monkeypatch.setattr(adapter.os, "killpg", lambda _pid, _signal: None)
    with pytest.raises(AgentError, match="cleanup timed out"):
        asyncio.run(adapter._reap(cast(Process, FixtureProcess())))
