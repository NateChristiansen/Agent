"""Resume versus restart, per transport: a Codex resume without its recorded thread used to start a
fresh thread with resume-only text, losing the worker contract and the work order."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli import repos
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_transport, lifecycle
from agent_cli.dispatch.resume_mode import resumes
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401


@pytest.mark.parametrize(
    "record,expected",
    [
        ({"attempt": 1, "session_established": True}, False),
        ({"attempt": 2, "session_established": True}, True),
        ({"attempt": 2, "session_established": False}, False),
        ({"attempt": 2, "last_result": {"x": 1}}, True),  # legacy record: a result proves a turn
        ({"attempt": 2}, False),
        ({"attempt": 2, "provider": "codex", "session_established": True, "codex_thread_id": "t1"}, True),
        ({"attempt": 2, "provider": "codex", "session_established": True}, False),
        ({"attempt": 2, "provider": "codex", "last_result": {"x": 1}}, False),
    ],
)
def test_resume_needs_an_observed_session_and_for_codex_its_thread(record: dict, expected: bool):
    assert resumes(record) is expected


def test_codex_resume_without_a_thread_id_fails_before_any_rpc(droot: Path, monkeypatch):
    d = _new_fast(droot)
    d["model"] = "gpt-6-luna"
    d.pop("codex_thread_id", None)
    repo = repos.load_registry(droot).require("alpha")

    @asynccontextmanager
    async def never(*_args: Any, **_kwargs: Any):
        raise AssertionError("no app-server, thread or turn may start")
        yield

    monkeypatch.setattr(codex_transport, "isolated_server", never)
    with pytest.raises(AgentError, match="records no codex_thread_id"):
        asyncio.run(
            codex_transport._run_codex(
                droot,
                d,
                repo,
                text="follow-up only",
                resume=True,
                mcp_servers={},
                inbox=droot / ".agent/scratch/absent-inbox",
                timeout=5,
            )
        )


def test_a_fresh_codex_start_sends_the_contract_and_the_work_order(droot: Path, monkeypatch):
    d = _new_fast(droot)
    d["model"] = "gpt-6-luna"
    repo = repos.load_registry(droot).require("alpha")
    calls: list[tuple[str, dict]] = []

    class RPC:
        process = None

        async def call(self, method: str, params: dict[str, Any]):
            calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "new-thread"}}
            raise AgentError("test stops before inference")

    @asynccontextmanager
    async def isolated(*_args: Any, **_kwargs: Any):
        yield RPC(), {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    asyncio.run(
        codex_transport._run_codex(
            droot,
            d,
            repo,
            text="THE WORK ORDER",
            resume=False,
            mcp_servers={},
            inbox=droot / ".agent/scratch/absent-inbox",
            timeout=5,
        )
    )
    assert [m for m, _ in calls] == ["thread/start", "turn/start"]
    sent = calls[1][1]["input"][0]["text"]
    assert "THE WORK ORDER" in sent and "Codex tool note" in sent


def test_a_codex_escalation_is_not_labelled_a_fork(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    old = _new_fast(droot)
    assert old["session_id"]
    captured: dict[str, Any] = {}
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: captured.setdefault("d", d))
    _new_fast(droot, escalate_from=old["id"], provider="codex", model="gpt-6-astra", effort="high")
    d = captured["d"]
    assert not d.get("forked_from_session")
    assert d.get("escalated_from")
