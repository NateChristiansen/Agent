"""Codex transport protocol and permission contract checks."""

from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, permissions, repos
from agent_cli.core import AgentError
from agent_cli.dispatch.codex_normalize import (
    codex_output_schema,
    result_envelope,
    structured_return,
    usage_counts,
)
from agent_cli.dispatch.codex_permissions import decision, git_metadata_refusal, run_event
from agent_cli.dispatch.codex_rpc import CodexAppServer
from agent_cli.dispatch.options import RETURN_SCHEMA
from agent_cli.dispatch.rules import build_settings
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- pytest fixture


def test_codex_rpc_routes_replies_notifications_and_requests(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(
        """import json
import sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'ping':
        print(json.dumps({'method': 'event', 'params': {'value': 3}}), flush=True)
        print(json.dumps({'id': 90, 'method': 'permission/request', 'params': {}}), flush=True)
        print(json.dumps({'id': message['id'], 'result': {'pong': True}}), flush=True)
    elif message.get('id') == 90:
        print(json.dumps({'method': 'answer', 'params': message['result']}), flush=True)
""",
        encoding="utf-8",
    )

    async def check() -> None:
        async with CodexAppServer(
            tmp_path,
            command=(sys.executable, str(server)),
            handle_request=lambda _request: asyncio.sleep(0, result={"decision": "decline"}),
        ) as rpc:
            assert await rpc.call("ping", {}) == {"pong": True}
            first = await asyncio.wait_for(rpc.events.get(), 1)
            second = await asyncio.wait_for(rpc.events.get(), 1)
            assert first == {"method": "event", "params": {"value": 3}}
            assert second == {"method": "answer", "params": {"decision": "decline"}}

    asyncio.run(check())


def test_codex_usage_and_cost_source() -> None:
    usage = {
        "tokenUsage": {
            "last": {"inputTokens": 20, "outputTokens": 5, "cachedInputTokens": 10},
            "total": {"totalTokens": 25},
        }
    }
    assert usage_counts(usage) == (
        {
            "input_tokens": 20,
            "output_tokens": 5,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 0,
        },
        25,
    )
    unknown = result_envelope(
        session_id="thread",
        text='{"status":"done"}',
        cost_usd=None,
        num_turns=1,
        status="completed",
        denials=[],
    )
    assert unknown["cost_source"] == "unknown"
    assert unknown["structured_output"] == {"status": "done"}


def test_codex_output_schema_requires_nullable_optional_fields() -> None:
    schema = codex_output_schema(RETURN_SCHEMA)
    assert schema["additionalProperties"] is False
    assert "comments" in schema["required"]
    assert schema["properties"]["comments"]["type"] == ["array", "null"]
    for gone in ("branch", "commits", "files_touched", "commands"):
        assert gone not in schema["properties"] and gone not in schema["required"]
    comment = schema["properties"]["comments"]["items"]
    assert comment["additionalProperties"] is False
    assert "end" in comment["required"]
    assert comment["properties"]["end"]["type"] == ["integer", "null"]
    assert structured_return('{"comments":[{"path":"a.py","line":1,"end":null,"reason":"r"}]}') == {
        "comments": [{"path": "a.py", "line": 1, "reason": "r"}]
    }


def test_codex_hook_allows_neutrally_and_denies_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Import before patching state.load: supervisor binds it at import time. Otherwise this
    # test leaves a patched binding in supervisor for later tests in the same worker.
    from agent_cli.dispatch import supervisor

    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.decision", lambda *_args: ("allow", "test"))
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.state.load", lambda *_args: {"id": "d"})
    monkeypatch.setattr(supervisor, "record_permission_event", lambda *_args: None)
    monkeypatch.setenv("AGENT_ROOT", "/tmp")
    monkeypatch.setenv("AGENT_DISPATCH_ID", "d")
    allowed = run_event({"tool_name": "Bash", "tool_input": {"command": "pwd"}})
    assert allowed == {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.decision", lambda *_args: ("deny", "blocked"))
    denied = run_event({"tool_name": "Bash", "tool_input": {"command": "x"}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == "blocked"


@pytest.mark.parametrize("decision_value", ["allow", "deny"])
def test_codex_hook_records_redacted_native_identity_without_arguments(
    monkeypatch: pytest.MonkeyPatch, decision_value: str
) -> None:
    from agent_cli.dispatch import supervisor

    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agent_cli.dispatch.codex_permissions.decision", lambda *_args: (decision_value, "policy")
    )
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.state.load", lambda *_args: {"id": "d"})
    monkeypatch.setattr(
        supervisor, "record_permission_event", lambda _root, _d, event: recorded.append(event)
    )
    monkeypatch.setenv("AGENT_ROOT", "/tmp")
    monkeypatch.setenv("AGENT_DISPATCH_ID", "d")

    run_event(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo private-argument"},
            "tool_use_id": "call_01HZZQ7N8M4Q5R6S7T8V9W0XYZ",
            "turn_id": "turn_01HZZQ7N8M4Q5R6S7T8V9W0XYZ",
        }
    )

    assert len(recorded) == 1
    event = recorded[0]
    assert event["tool_use_id"] == "[redacted]"
    assert event["turn_id"] == "[redacted]"
    assert event["decision"] == decision_value
    assert event["input"] == "[tool arguments omitted]"
    assert "private-argument" not in str(event)


@pytest.mark.parametrize(
    "identity",
    [
        {"tool_use_id": "", "turn_id": "turn"},
        {"tool_use_id": "https://user:secret@example.test/path", "turn_id": "Bearer secret"},
        {"tool_use_id": {"id": "call-1"}, "turn_id": ["turn-1"]},
        {"tool_use_id": "x" * 10000, "turn_id": "turn\\nwith-line-break"},
    ],
    ids=["empty", "secret-shaped", "non-string", "malformed"],
)
def test_codex_hook_never_records_unsafe_native_identity(
    monkeypatch: pytest.MonkeyPatch, identity: dict[str, Any]
) -> None:
    from agent_cli.dispatch import supervisor

    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.decision", lambda *_args: ("deny", "blocked"))
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.state.load", lambda *_args: {"id": "d"})
    monkeypatch.setattr(
        supervisor, "record_permission_event", lambda _root, _d, event: recorded.append(event)
    )
    monkeypatch.setenv("AGENT_ROOT", "/tmp")
    monkeypatch.setenv("AGENT_DISPATCH_ID", "d")

    run_event({"tool_name": "Bash", "tool_input": {"command": "private"}, **identity})

    assert len(recorded) == 1
    event = recorded[0]
    for field in ("tool_use_id", "turn_id"):
        if isinstance(identity.get(field), str):
            assert event[field] == "[redacted]"
        else:
            assert field not in event
    assert event["decision"] == "deny"
    assert event["input"] == "[tool arguments omitted]"
    assert "private" not in str(event)


def test_codex_hook_missing_native_identity_stays_unpaired(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cli.dispatch import supervisor

    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.decision", lambda *_args: ("allow", "policy"))
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.state.load", lambda *_args: {"id": "d"})
    monkeypatch.setattr(
        supervisor, "record_permission_event", lambda _root, _d, event: recorded.append(event)
    )
    monkeypatch.setenv("AGENT_ROOT", "/tmp")
    monkeypatch.setenv("AGENT_DISPATCH_ID", "d")

    run_event({"tool_name": "Bash", "tool_input": {"command": "private"}})

    assert len(recorded) == 1
    assert "tool_use_id" not in recorded[0]
    assert "turn_id" not in recorded[0]
    assert recorded[0]["input"] == "[tool arguments omitted]"


def test_git_metadata_approval_is_narrow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `_same_dir` needs an absolute cwd, and `/tmp/wt` has no drive on Windows.
    wt = str(tmp_path / "wt")
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.state.load", lambda *_args: {"worktree": wt})
    monkeypatch.setattr("agent_cli.dispatch.codex_permissions.decision", lambda *_args: ("allow", "rule"))
    base = {"kind": "command", "cwd": wt, "command": "git add README.md"}
    assert not git_metadata_refusal(tmp_path, "d", base)
    assert git_metadata_refusal(tmp_path, "d", {**base, "cwd": str(tmp_path / "other")})
    assert git_metadata_refusal(tmp_path, "d", {**base, "command": "git push origin main"})
    assert git_metadata_refusal(tmp_path, "d", {**base, "command": "git add README.md; touch /tmp/x"})
    assert git_metadata_refusal(tmp_path, "d", {**base, "networkApprovalContext": {}})


def test_codex_policy_uses_the_claude_worker_rules(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope(status="blocked"))])
    d = _new_fast(droot)
    repo = repos.load_registry(droot).require("alpha")
    rules = build_settings(droot, repo, worktree=d["worktree"])["permissions"]
    cases = {
        "git push origin main": "deny",
        'git commit -m "checkpoint"': "allow",
        "git add README.md": "allow",
        "unlisted-binary --unknown": "deny",
    }
    for command, expected in cases.items():
        claude_verdict, _rule = permissions.matches(command, rules["allow"], rules["deny"])
        assert expected == ("allow" if claude_verdict == "allowed" else "deny")
        codex_verdict, _reason = decision(
            droot, d["id"], {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert codex_verdict == expected, command
    chained, _reason = decision(
        droot, d["id"], {"tool_name": "Bash", "tool_input": {"command": "git status; git add README.md"}}
    )
    assert chained == "deny"
    mcp_tool = next(rule for rule in rules["allow"] if rule.startswith("mcp__"))
    allowed, _reason = decision(droot, d["id"], {"tool_name": mcp_tool, "tool_input": {}})
    assert allowed == "allow"


def test_codex_dispatch_uses_supervisor_worktree_and_result_contract(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.dispatch import codex_transport

    seen: list[dict] = []

    def fake_run(root: Path, d: dict, repo: object, **kwargs: object) -> dict:
        seen.append({"root": root, "id": d["id"], "worktree": d["worktree"], **kwargs})
        final = envelope(status="question", nonce=d["nonce"])
        final["session_id"] = d["session_id"]
        return {
            "last_result": final,
            "session_established": True,
            "exit_code": 0,
            "timed_out": False,
            "stop_reason": "result",
            "estimated_cost_usd": 0.01,
            "resolved_model": "gpt-6.1-sol",
            "codex_thread_id": "codex-thread",
        }

    monkeypatch.setattr(codex_transport, "run_codex", fake_run)
    d = _new_fast(droot, provider="codex", row="build", budget_reason="row band")
    assert len(seen) == 1
    assert seen[0]["id"] == d["id"]
    assert seen[0]["worktree"] == d["worktree"]
    assert isinstance(seen[0]["mcp_servers"], dict)
    assert dispatch.load(droot, d["id"])["codex_thread_id"] == "codex-thread"
    assert dispatch.load(droot, d["id"])["resolved_model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "model, message", [("gpt-future-9", "no verified price"), (None, "name a model or a row")]
)
def test_unpriced_codex_target_refuses_before_allocation(droot: Path, monkeypatch, dry_run, model, message):
    from agent_cli.dispatch import allocation, lifecycle

    monkeypatch.setattr(allocation, "allocate", lambda *_: pytest.fail("allocated unpriced worker"))
    monkeypatch.setattr(lifecycle, "start", lambda *_a, **_k: pytest.fail("started unpriced worker"))
    with pytest.raises(AgentError, match=message):
        _new_fast(droot, provider="codex", model=model, dry_run=dry_run)
    assert not list((droot / ".agent/dispatches").glob("*.json"))


@pytest.mark.parametrize("model", [None, "gpt-future-9"])
@pytest.mark.parametrize("resume", [False, True])
def test_transport_refuses_unpriced_target_before_server(droot: Path, monkeypatch, model, resume):
    from agent_cli.dispatch import codex_transport

    monkeypatch.setattr(codex_transport, "worker_env", lambda *_: pytest.fail("transport setup started"))
    with pytest.raises(AgentError, match="name a model or a row|no verified price"):
        codex_transport.run_codex(
            droot,
            {"model": model},
            repos.load_registry(droot).require("alpha"),
            text="x",
            resume=resume,
            mcp_servers={},
            inbox=droot / "inbox",
            timeout=1,
        )


@pytest.mark.parametrize("row", ["review", "build"])
def test_codex_owner_row_policy_reaches_preview_record_and_worker(droot: Path, monkeypatch, row):
    from agent_cli.dispatch import codex_transport

    seen = []

    def fake_run(root, record, repo, **kwargs):
        seen.append(record.copy())
        final = envelope(status="question", nonce=record["nonce"])
        final["session_id"] = record["session_id"]
        return {
            "last_result": final,
            "session_established": True,
            "exit_code": 0,
            "timed_out": False,
            "stop_reason": "result",
            "resolved_model": record["model"],
        }

    monkeypatch.setattr(codex_transport, "run_codex", fake_run)
    # A review row needs a target or base (079 #5).
    preview = _new_fast(droot, provider="codex", row=row, base="main", dry_run=True, budget_reason="row band")
    assert preview["model"] == "gpt-6.1-sol" and preview["effort"] == "medium"
    assert preview["effort_source"] == "row-policy" and preview["options"]["effort"] == "medium"
    assert "owner policy" in preview["choice_reason"]
    record = _new_fast(droot, provider="codex", row=row, base="main", budget_reason="row band")
    assert record["effort_source"] == "row-policy" and record["effort"] == "medium"
    assert seen[0]["model"] == "gpt-6.1-sol" and seen[0]["effort"] == "medium"


class CancelledRPC:
    """A turn that is cancelled before it completes. `after_interrupt` is what the turn reports
    as it closes, before its `turn/completed` (interrupted)."""

    def __init__(self, after_interrupt: list[dict[str, Any]]):
        self.process: Any = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[str] = []
        self.after_interrupt = after_interrupt
        self.prompts: list[str] = []

    def stderr_tail(self) -> str:
        return ""

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "thread/start":
            return {"thread": {"id": "cancel-thread"}}
        if method == "turn/start":
            self.prompts.append(params["input"][0]["text"])
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            for event in self.after_interrupt:
                await self.events.put(event)
            await self.events.put(
                {"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "interrupted"}}}
            )
        return {}


_USAGE_1000_200 = {
    "method": "thread/tokenUsage/updated",
    "params": {
        "tokenUsage": {"last": {"inputTokens": 1000, "outputTokens": 200}, "total": {"totalTokens": 1200}}
    },
}


def _cancel_codex_mid_run(droot: Path, monkeypatch: pytest.MonkeyPatch, rpc: CancelledRPC) -> dict[str, Any]:
    from contextlib import asynccontextmanager

    from agent_cli.core import iso, now
    from agent_cli.dispatch import codex_transport, driver, state
    from agent_cli.dispatch import verification_gate as gate

    @asynccontextmanager
    async def server(*_args: Any, **_kwargs: Any):
        yield rpc, {}

    def cancel_once_running(root: Path, dispatch_id: str) -> bool:
        if "turn/start" not in rpc.calls:
            return False
        with gate.record_guard(root, dispatch_id) as record:
            if record["state"] != "cancelled":
                record.update(
                    state="cancelled", cancel_requested=iso(now()), error="cancelled by the manager"
                )
                state.save(root, record)
        return True

    monkeypatch.setattr(codex_transport, "isolated_server", server)
    monkeypatch.setattr(driver, "cancel_requested", cancel_once_running)
    result = _new_fast(droot, provider="codex", model="gpt-6-sol", timeout_minutes=2)
    assert "turn/interrupt" in rpc.calls
    return dispatch.load(droot, result["id"])


def test_a_cancelled_codex_turn_records_the_usage_it_reports_on_interrupt(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import prices

    record = _cancel_codex_mid_run(droot, monkeypatch, CancelledRPC([_USAGE_1000_200]))
    expected = prices.price_usage("gpt-6-sol", {"input_tokens": 1000, "output_tokens": 200})
    assert record["state"] == "cancelled"
    assert record["cost_usd"] == pytest.approx(expected)
    assert record["last_result"]["cost_source"] == "local-estimate"
    assert "cost_basis" not in record["last_result"]


def test_usage_after_a_reroute_during_the_interrupt_is_priced_at_the_new_model(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import prices

    reroute = {"method": "model/rerouted", "params": {"fromModel": "gpt-6-sol", "toModel": "gpt-6-luna"}}
    record = _cancel_codex_mid_run(droot, monkeypatch, CancelledRPC([reroute, _USAGE_1000_200]))
    expected = prices.price_usage("gpt-6-luna", {"input_tokens": 1000, "output_tokens": 200})
    assert expected == pytest.approx(0.0002)
    assert record["cost_usd"] == pytest.approx(expected)


def test_a_cancelled_codex_turn_with_no_usage_records_a_marked_prompt_estimate(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.dispatch import codex_transport

    rpc = CancelledRPC([])
    record = _cancel_codex_mid_run(droot, monkeypatch, rpc)
    expected = codex_transport.prompt_estimate("gpt-6-sol", rpc.prompts[0])
    assert record["state"] == "cancelled"
    assert expected and record["cost_usd"] == pytest.approx(expected)
    assert record["last_result"]["cost_source"] == "local-estimate"
    assert record["last_result"]["cost_basis"] == codex_transport.PROMPT_ESTIMATE_BASIS
    assert record["cost_estimated_attempts"] == [1]


class _SupervisorKilled(BaseException):
    """Stands in for TerminateProcess: nothing in the supervisor catches it, so no drain, result
    or finalize of its own runs."""


class KilledRPC(CancelledRPC):
    def __init__(self, before_kill: list[dict[str, Any]]):
        super().__init__([])
        self.before_kill = before_kill

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        reply = await super().call(method, params)
        if method == "turn/start":
            for event in self.before_kill:
                await self.events.put(event)
        return reply


def _kill_supervisor_then_cancel(
    droot: Path, monkeypatch: pytest.MonkeyPatch, rpc: KilledRPC
) -> dict[str, Any]:
    """Windows W3's order: the supervisor dies mid-turn without persisting a result, and the
    manager's `cancel` finalizes from the record alone."""
    from contextlib import asynccontextmanager

    from agent_cli.dispatch import codex_transport, driver, state
    from agent_cli.dispatch import verification_gate as gate

    @asynccontextmanager
    async def server(*_args: Any, **_kwargs: Any):
        yield rpc, {}

    def killed_once_events_read(_root: Path, _dispatch_id: str) -> bool:
        if "turn/start" in rpc.calls and rpc.events.empty():
            raise _SupervisorKilled
        return False

    monkeypatch.setattr(codex_transport, "isolated_server", server)
    monkeypatch.setattr(driver, "cancel_requested", killed_once_events_read)
    with pytest.raises(_SupervisorKilled):
        _new_fast(droot, provider="codex", model="gpt-6-sol", timeout_minutes=2)
    (running,) = dispatch.load_all(droot)
    assert running["state"] == "running" and not running.get("last_result")
    with gate.record_guard(droot, running["id"]) as record:
        record["supervisor_pid"] = None
        state.save(droot, record)
    # Nothing can settle a supervisor that died before writing its evidence, so cancel's
    # settle loop only spends its full 8 s wait before giving up; one pass reaches the same end.
    monkeypatch.setattr(gate, "settle", functools.partial(gate.settle, timeout=0))
    dispatch.cancel(droot, dispatch_id=running["id"])
    return dispatch.load(droot, running["id"])


def test_a_codex_cancel_after_the_supervisor_is_killed_records_the_prompt_estimate(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.dispatch import codex_transport

    rpc = KilledRPC([])
    record = _kill_supervisor_then_cancel(droot, monkeypatch, rpc)
    expected = codex_transport.prompt_estimate("gpt-6-sol", rpc.prompts[0])
    assert record["state"] == "cancelled"
    assert expected and record["cost_usd"] == pytest.approx(expected)
    assert record["cost_evidence"] == {"1": "estimate"}
    assert record["cost_estimated_attempts"] == [1]


def test_a_codex_cancel_after_the_supervisor_is_killed_records_the_usage_seen(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli import prices

    record = _kill_supervisor_then_cancel(droot, monkeypatch, KilledRPC([_USAGE_1000_200]))
    expected = prices.price_usage("gpt-6-sol", {"input_tokens": 1000, "output_tokens": 200})
    assert record["state"] == "cancelled"
    assert record["cost_usd"] == pytest.approx(expected)
    assert record["cost_evidence"] == {"1": "estimate"}


@pytest.mark.parametrize("model", [None, "gpt-future-9"])
def test_resume_refuses_unpriced_codex_before_new_attempt(droot: Path, fake_claude, monkeypatch, model):
    from agent_cli.dispatch import lifecycle

    fake_claude.scenario([one(envelope(status="question"))])
    record = _new_fast(droot)
    record.update(provider="codex", model=model)
    dispatch.save(droot, record)
    monkeypatch.setattr(lifecycle, "start", lambda *_a, **_k: pytest.fail("started resumed worker"))
    with pytest.raises(AgentError, match="name a model or a row|no verified price"):
        dispatch.resume(droot, dispatch_id=record["id"], answer="continue")
    assert dispatch.load(droot, record["id"])["attempt"] == 1
