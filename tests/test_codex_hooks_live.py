"""Opt-in live probe: two bounded turns, no requested file edits."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from agent_cli import prices, repos
from agent_cli.dispatch import codex_hooks, codex_transport, state
from agent_cli.dispatch.codex_normalize import usage_counts
from dispatch_helpers import _new_fast, droot  # noqa: F401 -- pytest fixture

pwd = pytest.importorskip("pwd", reason="POSIX-only: the probe finds the real HOME through pwd")


@pytest.mark.skipif(os.getenv("AGENT_CODEX_B0F_PROBE") != "1", reason="owner-authorized B0f live probe")
def test_live_project_isolation_and_generated_policy_on_resume(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = Path(__file__).resolve().parents[1]
    resume_thread = os.getenv("AGENT_CODEX_B0F_RESUME_THREAD", "")
    if resume_thread:
        droot = Path(os.environ["AGENT_CODEX_B0F_FIXTURE_ROOT"])
        d = state.load(droot, "001-mac-01")
        assert d["worktree"] == str(actual)
    else:
        d = _new_fast(droot)
        d["worktree"] = str(actual)
        state.save(droot, d)  # Only this test's temporary fixture, never a real dispatch.
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    env = dict(os.environ, AGENT_ROOT=str(droot), AGENT_DISPATCH_ID=d["id"], PYTHONPATH=str(actual / "src"))
    protected = [actual / "adrs/README.md", actual / "STATUS.md"]
    before = {path: path.read_bytes() if path.exists() else None for path in protected}
    repo = repos.Repo(name="Harness", short="harness", path=str(actual), remote="")
    estimates: list[float] = []

    async def deny(_request: dict[str, Any]) -> dict[str, Any]:
        return {"decision": "decline"}

    async def turn(rpc: Any, thread: str) -> None:
        started = await rpc.call(
            "turn/start",
            {
                "threadId": thread,
                "input": [
                    {
                        "type": "text",
                        "text": "Use the shell tool to run git status --short. Then make a separate shell call "
                        "to run curl https://example.com. Attempt both calls even if a previous turn "
                        "already denied curl. Report each result. Do not edit files or run any other command.",
                    }
                ],
                "approvalPolicy": "on-request",
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": codex_transport._writable_roots(actual),
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": False,
                    "excludeSlashTmp": False,
                },
            },
        )
        turn_id = started["turn"]["id"]
        tokens, cost = -1, 0.0
        successful_status = False
        completed = False
        try:
            while True:
                event = await rpc.events.get()
                method = event.get("method")
                assert method != "connection/closed"
                if method == "thread/tokenUsage/updated":
                    row = usage_counts(event.get("params", {}))
                    if row and row[1] > tokens:
                        amount = prices.price_usage("gpt-6-luna", row[0])
                        assert amount is not None, "probe pricing unavailable"
                        cost += amount
                        tokens = row[1]
                        assert cost < 0.5, "probe soft budget exceeded"
                if method == "item/completed":
                    item = event.get("params", {}).get("item", {})
                    if item.get("type") == "commandExecution":
                        if "git status --short" in item.get("command", ""):
                            successful_status = item.get("exitCode") == 0
                if method == "turn/completed":
                    completed = True
                    assert event["params"]["turn"]["status"] == "completed"
                    assert successful_status, "allowed git status did not complete successfully"
                    estimates.append(cost)
                    return
        finally:
            if not completed:
                await rpc.call("turn/interrupt", {"threadId": thread, "turnId": turn_id})

    async def check() -> None:
        thread = resume_thread
        for resume in (True,) if resume_thread else (False, True):
            offset = len(state.load(droot, d["id"]).get("permission_events", []))
            async with codex_hooks.isolated_server(actual, repo, actual, env=env, handle_request=deny) as (
                rpc,
                hooks,
            ):
                params: dict[str, Any] = {
                    "cwd": str(actual),
                    "model": "gpt-6-luna",
                    "approvalPolicy": "on-request",
                    "sandbox": "workspace-write",
                    "config": {
                        "hooks": hooks,
                        "bypass_hook_trust": True,
                        "agents": {"enabled": False},
                        "features": {"multi_agent": False},
                    },
                }
                if resume:
                    params["threadId"] = thread
                result = await rpc.call("thread/resume" if resume else "thread/start", params)
                thread = result["thread"]["id"]
                await asyncio.wait_for(turn(rpc, thread), 90)
            events = state.load(droot, d["id"]).get("permission_events", [])[offset:]
            assert any(e["tool"] == "Bash" and e["decision"] == "allow" for e in events), events
            assert any(e["tool"] == "Bash" and e["decision"] == "deny" for e in events), events
            assert {path: path.read_bytes() if path.exists() else None for path in protected} == before
            print(
                {
                    "resumed": resume,
                    "allow": sum(e["decision"] == "allow" for e in events),
                    "deny": sum(e["decision"] == "deny" for e in events),
                    "estimated_usd": estimates[-1],
                    "generated_views_unchanged": True,
                }
            )

    asyncio.run(asyncio.wait_for(check(), 210))
