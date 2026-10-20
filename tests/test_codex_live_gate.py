"""Opt-in live probe for the Codex hook launch gate.

Run with AGENT_CODEX_LIVE_PROBE=1. This consumes one small Codex turn and writes
only under pytest's temporary directory.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli.dispatch.codex_rpc import CodexAppServer
from dispatch_helpers import _new_fast, droot  # noqa: F401 -- pytest fixture

pwd = pytest.importorskip("pwd", reason="POSIX-only: the probe finds the real HOME through pwd")


@pytest.mark.skipif(os.getenv("AGENT_CODEX_LIVE_PROBE") != "1", reason="opt-in live Codex probe")
def test_codex_hook_denies_shell_command(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    denied_command = tmp_path / "denied.py"
    denied_command.write_text(
        """import json
import sys
from pathlib import Path
event = json.load(sys.stdin)
Path(__file__).with_suffix('.event').write_text(json.dumps(event))
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': 'Codex dispatch denial probe'}}))
""",
        encoding="utf-8",
    )
    marker = tmp_path / "must-not-exist"
    resumed_marker = tmp_path / "must-not-exist-after-resume"
    config = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"{sys.executable} {denied_command}"}],
                }
            ]
        },
        "bypass_hook_trust": True,
    }

    async def turn(rpc: CodexAppServer, thread: str, target: Path) -> None:
        await rpc.call(
            "turn/start",
            {
                "threadId": thread,
                "input": [
                    {
                        "type": "text",
                        "text": f"Use your shell tool to run exactly this command: touch {target}. Then report the result.",
                    }
                ],
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(tmp_path)],
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": False,
                    "excludeSlashTmp": False,
                },
            },
        )
        for _ in range(90):
            event = await asyncio.wait_for(rpc.events.get(), 30)
            if event.get("method") == "turn/completed":
                return
        pytest.fail("Codex turn did not complete")

    async def check() -> None:
        async with CodexAppServer(tmp_path) as rpc:
            listed = await rpc.call("hooks/list", {"cwds": [str(tmp_path)]})
            assert len(listed["data"]) == 1
            assert not listed["data"][0]["hooks"]
            started = await rpc.call(
                "thread/start",
                {
                    "cwd": str(tmp_path),
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "config": config,
                },
            )
            thread = started["thread"]["id"]
            await turn(rpc, thread, marker)
        assert denied_command.with_suffix(".event").exists(), "PreToolUse hook did not run"
        denied_command.with_suffix(".event").unlink()
        async with CodexAppServer(tmp_path) as rpc:
            await rpc.call(
                "thread/resume",
                {"threadId": thread, "cwd": str(tmp_path), "sandbox": "workspace-write", "config": config},
            )
            await turn(rpc, thread, resumed_marker)

    asyncio.run(check())
    assert denied_command.with_suffix(".event").exists(), "PreToolUse hook did not run"
    assert not marker.exists(), "PreToolUse denial did not block the command"
    assert not resumed_marker.exists(), "PreToolUse denial did not survive resume"


@pytest.mark.skipif(os.getenv("AGENT_CODEX_LIVE_PROBE") != "1", reason="opt-in live Codex probe")
def test_codex_full_dispatch_lifecycle(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cli import dispatch

    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    # tmp_root's bin/agent is a deliberately inert fixture stub. Point its
    # worker MCP command at this test run's installed CLI instead.
    agent_exe = Path(sys.executable).with_name("agent")
    stub = droot / "bin" / "agent"
    stub.write_text(f'#!/bin/sh\nexec "{agent_exe}" "$@"\n', encoding="utf-8")
    stub.chmod(0o755)
    record = _new_fast(
        droot,
        provider="codex",
        row="mechanical",
        budget_reason="live integration probe",
        prompt="Add one line saying 'Alpha is a test service.' to README.md, commit it, and report the result.",
    )
    final = dispatch.load(droot, record["id"])
    assert final["provider"] == "codex"
    assert final["codex_thread_id"]
    assert final["resolved_model"]
    assert final["last_result"] is not None
    assert final["last_result"]["cost_source"] in ("local-estimate", "unknown")
    assert final["last_result"]["git_metadata_approvals"] >= 2
    assert final["state"] == "done", final.get("error")
