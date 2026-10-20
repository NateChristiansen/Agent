"""Codex app-server framing: large replies, notifications and resumed histories (phase A).

On 2026-10-07 every Codex resume died on `ValueError: Separator is not found, and chunk exceed the
limit`: the reader kept asyncio's default 64 KiB line limit and a resumed thread's history arrives
as one JSON line. These run a fake app-server that writes large frames in fragments.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli import repos
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_transport
from agent_cli.dispatch.codex_rpc import FRAME_LIMIT, STDERR_TAIL, CodexAppServer
from agent_cli.dispatch.state import file
from dispatch_helpers import _new_fast, droot  # noqa: F401 -- pytest fixture

SERVER = """import json
import sys
import time

def emit(message):
    data = json.dumps(message) + "\\n"
    step = 50000
    for start in range(0, len(data), step):  # fragmented writes, like a pipe under load
        sys.stdout.write(data[start:start + step])
        sys.stdout.flush()
        time.sleep(0.0005)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        emit({"id": message["id"], "result": {}})
    elif method == "big":
        size = message["params"]["size"]
        emit({"method": "item/completed", "params": {"text": "n" * size}})
        emit({"id": message["id"], "result": {"text": "r" * size}})
    elif method == "thread/resume":
        turns = [{"id": f"turn-{i}", "items": [{"type": "agentMessage", "text": "h" * 4000}]} for i in range(400)]
        emit({"id": message["id"], "result": {"thread": {"id": "t1", "turns": turns}}})
    elif method == "turn/start":
        emit({"id": message["id"], "result": {"turn": {"id": "turn-resumed"}}})
        emit({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": json.dumps({"status": "done", "summary": "resumed terminal result"})}}})
        emit({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
"""


def _server(tmp_path: Path) -> tuple[str, ...]:
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    return (sys.executable, str(script))


def test_the_frame_limit_is_deliberate_and_bounded() -> None:
    assert 16 * 1024 * 1024 <= FRAME_LIMIT <= 256 * 1024 * 1024


@pytest.mark.parametrize("size", [128 * 1024, 3 * 1024 * 1024])
def test_large_replies_and_notifications_arrive_whole(tmp_path: Path, size: int) -> None:
    async def check() -> None:
        async with CodexAppServer(tmp_path, command=_server(tmp_path)) as rpc:
            reply = await rpc.call("big", {"size": size})
            assert reply == {"text": "r" * size}
            event = await asyncio.wait_for(rpc.events.get(), 5)
            assert event == {"method": "item/completed", "params": {"text": "n" * size}}

    asyncio.run(check())


def test_a_resumed_history_larger_than_the_old_limit_is_read(tmp_path: Path) -> None:
    async def check() -> None:
        async with CodexAppServer(tmp_path, command=_server(tmp_path)) as rpc:
            resumed = await rpc.call("thread/resume", {"threadId": "t1"})
            assert len(resumed["thread"]["turns"]) == 400

    asyncio.run(check())


def test_an_oversized_frame_fails_once_with_its_method_and_size(tmp_path: Path) -> None:
    async def check() -> tuple[str, dict[str, object]]:
        async with CodexAppServer(tmp_path, command=_server(tmp_path), frame_limit=256 * 1024) as rpc:
            with pytest.raises(AgentError) as caught:
                await rpc.call("big", {"size": 1024 * 1024})
            closed = await asyncio.wait_for(rpc.events.get(), 5)
            assert rpc.failure is not None and str(rpc.failure) == str(caught.value)
        return str(caught.value), closed

    message, closed = asyncio.run(check())  # cleanup must not rethrow the reader's failure
    assert "frame over 262144 bytes" in message
    assert "pending: big" in message
    assert "closed its output" not in message
    assert closed == {"method": "connection/closed", "params": {"error": message}}


def test_an_ordinary_close_still_reports_closed_output(tmp_path: Path) -> None:
    script = tmp_path / "quits.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'initialize':\n"
        "        print(json.dumps({'id': m['id'], 'result': {}}), flush=True)\n"
        "    else:\n"
        "        sys.exit(0)\n",
        encoding="utf-8",
    )

    async def check() -> None:
        async with CodexAppServer(tmp_path, command=(sys.executable, str(script))) as rpc:
            with pytest.raises(AgentError, match="closed its output"):
                await rpc.call("anything", {})
            assert rpc.failure is None
            assert await asyncio.wait_for(rpc.events.get(), 5) == {
                "method": "connection/closed",
                "params": {},
            }

    asyncio.run(check())


def test_the_stderr_tail_is_kept_and_bounded(tmp_path: Path) -> None:
    script = tmp_path / "noisy.py"
    script.write_text(
        "import json, sys\n"
        "sys.stderr.write('x' * 20000 + 'LAST LINE\\n'); sys.stderr.flush()\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if 'id' in m:\n"
        "        print(json.dumps({'id': m['id'], 'result': {}}), flush=True)\n",
        encoding="utf-8",
    )

    async def check() -> str:
        async with CodexAppServer(tmp_path, command=(sys.executable, str(script))) as rpc:
            await rpc.call("ping", {})
            for _ in range(100):
                if "LAST LINE" in rpc.stderr_tail():
                    break
                await asyncio.sleep(0.01)
            return rpc.stderr_tail()

    tail = asyncio.run(check())
    assert tail.endswith("LAST LINE")
    assert len(tail) <= STDERR_TAIL


def test_a_failed_resume_records_its_error_and_stderr_once(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _new_fast(droot)
    d.update(codex_thread_id="existing-thread", model="gpt-6-luna")
    repo = repos.load_registry(droot).require("alpha")
    failure = (
        "Codex app-server sent a frame over 67108864 bytes (at least 70000000 read; pending: thread/resume)"
    )

    class RPC:
        process = None

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise AgentError(failure)

        def stderr_tail(self) -> str:
            return "codex: resume history is large"

    @asynccontextmanager
    async def isolated(*_args: Any, **_kwargs: Any) -> AsyncIterator[tuple[RPC, dict[str, Any]]]:
        yield RPC(), {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    result = asyncio.run(
        codex_transport._run_codex(
            droot,
            d,
            repo,
            text="fix the finding",
            resume=True,
            mcp_servers={},
            inbox=droot / ".agent/scratch/absent-inbox",
            timeout=5,
        )
    )
    err = file(droot, d["id"], ".err").read_text(encoding="utf-8")
    assert err.count("Codex transport error:") == 1
    assert f"AgentError: {failure}" in err
    assert "Codex app-server stderr (tail):\ncodex: resume history is large" in err
    assert result["stop_reason"] == "error"


def test_a_read_error_reaches_pending_calls_once_and_cleanup_is_quiet(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text(
        "import json, sys, time\n"
        "m = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id': m['id'], 'result': {}}), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    async def check() -> str:
        rpc = CodexAppServer(tmp_path, command=(sys.executable, str(script)))
        async with rpc:
            pending = asyncio.create_task(rpc.call("waiting", {}))
            await asyncio.sleep(0.05)
            assert rpc.process is not None and rpc.process.stdout is not None
            rpc.process.stdout.set_exception(OSError("synthetic pipe failure"))
            with pytest.raises(AgentError) as caught:
                await pending
        return str(caught.value)

    message = asyncio.run(check())  # leaving the context must not rethrow the OSError
    assert message == "Codex app-server output failed: OSError: synthetic pipe failure"


def test_a_failed_initialize_reaps_the_child(tmp_path: Path) -> None:
    script = tmp_path / "huge_init.py"
    script.write_text(
        "import os, sys, time\nsys.stdin.readline()\nos.write(1, b'x' * 9000 + b'\\n')\ntime.sleep(30)\n",
        encoding="utf-8",
    )

    async def check() -> int | None:
        rpc = CodexAppServer(tmp_path, command=(sys.executable, str(script)), frame_limit=8192)
        with pytest.raises(AgentError, match="frame over 8192 bytes"):
            async with rpc:
                pass
        assert rpc.process is not None
        return rpc.process.returncode

    assert asyncio.run(check()) is not None


def test_partial_eof_refuses_the_frame_and_future_calls_with_the_same_cause(tmp_path: Path) -> None:
    script = tmp_path / "partial.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'initialize':\n"
        "        print(json.dumps({'id': m['id'], 'result': {}}), flush=True)\n"
        "    elif 'id' in m:\n"
        "        sys.stdout.write('{\\\"id\\\":'); sys.stdout.flush(); sys.exit(0)\n",
        encoding="utf-8",
    )

    async def check() -> None:
        async with CodexAppServer(tmp_path, command=(sys.executable, str(script))) as rpc:
            with pytest.raises(AgentError, match="unterminated frame") as first:
                await rpc.call("resume", {})
            with pytest.raises(AgentError) as late:
                await asyncio.wait_for(rpc.call("late", {}), 0.1)
            assert first.value is late.value is rpc.failure
            assert "pending: resume" in str(first.value)
            assert not rpc._pending

    asyncio.run(check())


def test_calls_after_an_ordinary_close_fail_immediately(tmp_path: Path) -> None:
    async def check() -> None:
        rpc = CodexAppServer(tmp_path)
        rpc._closed_error = AgentError("Codex app-server closed its output")
        with pytest.raises(AgentError, match="closed its output"):
            await asyncio.wait_for(rpc.call("late", {}), 0.1)
        assert not rpc._pending

    asyncio.run(check())


def test_large_resume_reaches_a_terminal_result_on_the_original_dispatch(
    droot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.dispatch.state import load

    d = _new_fast(droot)
    d.update(codex_thread_id="t1", model="gpt-6-luna")
    repo = repos.load_registry(droot).require("alpha")
    methods: list[str] = []

    @asynccontextmanager
    async def isolated(*_args: Any, **_kwargs: Any) -> AsyncIterator[tuple[CodexAppServer, dict[str, Any]]]:
        async with CodexAppServer(tmp_path, command=_server(tmp_path)) as rpc:
            original = rpc.call

            async def capture(method: str, params: dict[str, Any]) -> dict[str, Any]:
                methods.append(method)
                return await original(method, params)

            monkeypatch.setattr(rpc, "call", capture)
            yield rpc, {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    result = asyncio.run(
        codex_transport._run_codex(
            droot,
            d,
            repo,
            text="continue review",
            resume=True,
            mcp_servers={},
            inbox=droot / ".agent/scratch/absent-inbox",
            timeout=5,
        )
    )
    assert methods == ["thread/resume", "turn/start"]
    assert result["stop_reason"] == "result"
    assert result["last_result"]["structured_output"]["status"] == "done"
    saved = load(droot, d["id"])
    assert saved["id"] == d["id"]
    assert saved["last_result"]["structured_output"]["summary"] == "resumed terminal result"


def test_the_recorded_stderr_and_error_are_redacted(tmp_path: Path) -> None:
    secret = "FAKE_TOKEN_FOR_THIS_TEST_ONLY_0123456789"

    class Server:
        def stderr_tail(self) -> str:
            return f"Authorization: Bearer {secret}\nOPENAI_API_KEY={secret}\nplain diagnostic"

    file(tmp_path, "probe", ".err").parent.mkdir(parents=True, exist_ok=True)
    codex_transport._transport_failure(tmp_path, "probe", f"failed with token {secret}", Server())  # pyright: ignore[reportArgumentType]
    err = file(tmp_path, "probe", ".err").read_text(encoding="utf-8")
    assert secret not in err
    assert "plain diagnostic" in err
    assert err.count("Codex transport error:") == 1
