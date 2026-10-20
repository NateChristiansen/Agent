# ruff: noqa
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Any
from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)
from .. import launcher
from ..append_log import open_append
from ..core import AgentError, is_alive, iso, now
from . import notice_producers, streams
from . import state as state_mod
from . import usage as usage_mod
from .isolated_transport import IsolatedClaudeTransport
from .normalize import out_record, result_envelope, return_status
from .state import CANCEL_GRACE_SECONDS, file, load, save


def detach(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    from . import verification_shell
    from .env_names import claude_named
    from .supervisor import popen

    err_path = file(root, d["id"], ".err")
    env = {k: v for k, v in os.environ.items() if not claude_named(k) and k != verification_shell.CARRIED}
    env |= verification_shell.carried()
    env["AGENT_ROOT"] = str(root)
    env["AGENT_DISPATCH_ID"] = str(d["id"])
    argv = [*launcher.argv_prefix(root), "dispatch", "run", d["id"]]
    with open_append(err_path) as err:
        proc, breakaway = launcher.spawn_supervisor(
            popen, argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=err, stderr=err
        )
    if breakaway is not None:
        d["supervisor_breakaway"] = breakaway
    d["supervisor_pid"] = proc.pid
    return save(root, d)


def run_worker(
    root: Path,
    d: dict[str, Any],
    options: ClaudeAgentOptions,
    *,
    first_message: str,
    inbox: Path,
    timeout: float,
    idle_seconds: float | None = None,
) -> dict[str, Any]:
    file(root, d["id"], ".out").parent.mkdir(parents=True, exist_ok=True)
    streams.keep_previous(root, d)
    return asyncio.run(
        _drive_worker(
            root,
            d,
            options,
            first_message=first_message,
            inbox=inbox,
            timeout=timeout,
            idle_seconds=state_mod.QUESTION_IDLE_SECONDS if idle_seconds is None else idle_seconds,
        )
    )


async def _drive_worker(
    root: Path,
    d: dict[str, Any],
    options: ClaudeAgentOptions,
    *,
    first_message: str,
    inbox: Path,
    timeout: float,
    idle_seconds: float,
) -> dict[str, Any]:
    from .supervisor import remember_worker_pid, transport_factory

    out_path, err_path = (file(root, d["id"], ".out"), file(root, d["id"], ".err"))
    state: dict[str, Any] = {
        "last_result": None,
        "session_established": False,
        "timed_out": False,
        "stop_reason": None,
        **usage_mod.initial_state(d),
    }
    stop = asyncio.Event()
    with (
        out_path.open("w", encoding="utf-8", buffering=1) as out,
        err_path.open("a", encoding="utf-8", buffering=1) as err,
    ):
        note(
            err,
            f"launch: {options.cli_path} in {options.cwd} ({('resume' if options.resume else 'session')} {d['session_id']})",
        )
        client: ClaudeSDKClient | None = None
        transport: Any = None
        startup_stage = "transport construction"
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", CanUseToolShadowedWarning)
                transport = (
                    transport_factory(options) if transport_factory else IsolatedClaudeTransport(options)
                )
                client = ClaudeSDKClient(options=options, transport=transport)
                startup_stage = "connect"
                await client.connect()
            remember_worker_pid(root, d, client)
            startup_stage = "first query"
            await client.query(first_message)
            startup_stage = "worker stream"
            reader = asyncio.create_task(read_worker(root, d, client, state, stop, out, err, idle_seconds))
            poller = asyncio.create_task(poll_worker(client, root, d, state, stop, inbox, err, timeout))
            await stop.wait()
            for task in (reader, poller):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        except Exception as exc:
            stage = (
                getattr(transport, "startup_stage", startup_stage)
                if startup_stage == "connect"
                else startup_stage
            )
            note(err, f"transport error during {stage}: {type(exc).__name__}: {exc!r}")
            state["stop_reason"] = state["stop_reason"] or "error"
        finally:
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(), state_mod.WATCHDOG_SECONDS)
                except Exception as exc:
                    note(err, f"cleanup failed: {exc}")
                    state["stop_reason"] = "cleanup_failed"
        note(err, f"worker stopped: {state['stop_reason']}")
    result = state["last_result"]
    return {
        "last_result": result,
        "session_established": bool(state["session_established"]),
        "exit_code": 0
        if result is not None and (not result.get("is_error")) and (state["stop_reason"] != "cleanup_failed")
        else 1,
        "timed_out": bool(state["timed_out"]),
        "stop_reason": state["stop_reason"],
        "estimated_cost_usd": float(state.get("estimated_cost_usd") or 0.0),
    }


def _persist_resolved_model(root: Path, dispatch_id: str, model: str) -> None:
    from . import verification_gate as gate

    with gate.record_guard(root, dispatch_id) as latest:
        latest["resolved_model"] = model
        save(root, latest)


async def read_worker(
    root: Path,
    d: dict[str, Any],
    client: ClaudeSDKClient,
    state: dict[str, Any],
    stop: asyncio.Event,
    out: Any,
    err: Any,
    idle_seconds: float,
) -> None:
    from .supervisor import persist_result

    try:
        async for msg in client.receive_messages():
            out.write(json.dumps(out_record(msg), default=str) + "\n")
            if isinstance(msg, (SystemMessage, AssistantMessage, ResultMessage)):
                state["session_established"] = True
            if not isinstance(msg, ResultMessage):
                if isinstance(msg, AssistantMessage):
                    state["answer_wait_until"] = None
                    if state.get("question_deadline") is not None:
                        state["question_deadline"] = None
                        state["first_result_at"] = None
                        note(err, "worker is working again after its question; hold cleared")
                        await asyncio.to_thread(notice_producers.answered, root, str(d["id"]))
                    usage_mod.count_message(root, str(d["id"]), msg, state)
                    if msg.model and (not state.get("resolved_model")):
                        state["resolved_model"] = str(msg.model)
                        _persist_resolved_model(root, str(d["id"]), str(msg.model))
                continue
            envelope = result_envelope(msg)
            if _is_queue_drain(envelope):
                out.flush()
                note(err, "empty result (queued notifications drained); waiting for the worker's return")
                continue
            state["last_result"] = envelope
            persist_result(root, d["id"], envelope)
            state["first_result_at"] = time.monotonic()
            status = return_status(envelope)
            answered_early, state["answer_pending"] = (bool(state.get("answer_pending")), False)
            if status == "question" and answered_early:
                state["first_result_at"] = None
                state["answer_wait_until"] = time.monotonic() + idle_seconds
                note(
                    err, "worker asked a question its delivered answer already covers; waiting for it to act"
                )
                continue
            if status == "question":
                state["question_deadline"] = time.monotonic() + idle_seconds
                note(err, f"worker asked a question; holding {idle_seconds:.0f}s for an answer")
                await asyncio.to_thread(notice_producers.question_hold, root, str(d["id"]))
                continue
            note(err, f"worker returned {status or envelope.get('subtype') or '?'}")
            state["stop_reason"] = state["stop_reason"] or "result"
            break
    finally:
        usage_mod.count(root, str(d["id"]), None, state, force=True)
        state["stop_reason"] = state["stop_reason"] or "stream-end"
        stop.set()


async def poll_worker(
    client: ClaudeSDKClient,
    root: Path,
    d: dict[str, Any],
    state: dict[str, Any],
    stop: asyncio.Event,
    inbox: Path,
    err: Any,
    timeout: float,
) -> None:
    from ..vault import live

    deadline = time.monotonic() + timeout
    beat = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(state_mod.POLL_SECONDS)
        clock = time.monotonic()
        if clock - beat >= live.HEARTBEAT_SECONDS:
            beat = clock
            await asyncio.to_thread(live.write_quietly, root, heartbeat=True)
        watchdog = _watchdog_deadline(state)
        if watchdog is not None and clock > watchdog:
            note(
                err, f"watchdog: {state_mod.WATCHDOG_SECONDS:.0f}s past the worker's last decision; stopping"
            )
            return _stop(state, stop, "watchdog")
        if cancel_requested(root, d["id"]):
            note(err, "cancelled by the manager; interrupting")
            await _interrupt(client, err)
            return _stop(state, stop, "cancelled")
        if clock > deadline:
            note(err, f"timeout after {timeout:.0f}s; interrupting")
            state["timed_out"] = True
            await _interrupt(client, err)
            return _stop(state, stop, "timeout")
        message = take_inbox(inbox)
        if message:
            note(err, f"steer delivered ({len(message)} chars)")
            if asked := (state.get("question_deadline") is not None):
                await asyncio.to_thread(notice_producers.answered, root, str(d["id"]))
            await client.query(message)
            if not asked and state.get("question_deadline") is not None:
                await asyncio.to_thread(notice_producers.answered, root, str(d["id"]))
            elif not asked:
                state["answer_pending"] = True
            state["question_deadline"] = None
            state["first_result_at"] = None
            continue
        held = state.get("question_deadline")
        if held is not None and clock > held:
            note(err, "no answer to the question; finalizing")
            return _stop(state, stop, "idle")
        acting = state.get("answer_wait_until")
        if acting is not None and clock > acting:
            note(err, "the worker did not act on the delivered answer; finalizing")
            return _stop(state, stop, "idle")


def _is_queue_drain(envelope: dict[str, Any]) -> bool:
    return (
        envelope.get("structured_output") is None
        and (not envelope.get("is_error"))
        and (int(envelope.get("num_turns") or 0) == 0)
        and (str(envelope.get("subtype") or "") == "success")
    )


def _watchdog_deadline(state: dict[str, Any]) -> float | None:
    first = state.get("first_result_at")
    if first is None:
        return None
    held = state.get("question_deadline")
    return max(first, held or first) + state_mod.WATCHDOG_SECONDS


def _stop(state: dict[str, Any], stop: asyncio.Event, reason: str) -> None:
    state["stop_reason"] = reason
    stop.set()


async def _interrupt(client: ClaudeSDKClient, err: Any) -> None:
    try:
        await asyncio.wait_for(client.interrupt(), CANCEL_GRACE_SECONDS)
    except Exception as exc:
        note(err, f"interrupt was refused or timed out: {exc}")


def cancel_requested(root: Path, dispatch_id: str) -> bool:
    try:
        return load(root, dispatch_id).get("state") == "cancelled"
    except AgentError, OSError:
        return False


def note(err: Any, text: str) -> None:
    err.write(f"--- {iso(now())} {text}\n")


def take_inbox(inbox: Path | None) -> str:
    if inbox is None or not inbox.exists():
        return ""
    try:
        text = inbox.read_text(encoding="utf-8").strip()
        if text:
            inbox.write_text("", encoding="utf-8")
        return text
    except OSError:
        return ""


def _signal_group(pid: int, sig: int) -> bool:
    if sys.platform == "win32":
        return False
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError, PermissionError:
        return False
    return True


def kill_group(pid: int, *, root: Path | None = None, dispatch_id: str = "") -> list[int]:
    if sys.platform == "win32":
        from .. import procs

        targets, unknown = (
            procs.select_windows_reap(dispatch=dispatch_id, leader_pid=pid, root=root)
            if dispatch_id
            else ([], [])
        )
        if root is not None and targets:
            procs.reap(root, targets)
        return [p.pid for p in unknown]
    if not _signal_group(pid, signal.SIGTERM):
        return []
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return []
        time.sleep(0.05)
    _signal_group(pid, signal.SIGKILL)
    return []


def await_worker_stop(root: Path, dispatch_id: str) -> bool:
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while time.monotonic() < deadline:
        d = load(root, dispatch_id)
        if d.get("stop_reason") is not None:
            return True
        if not is_alive(d.get("supervisor_pid")):
            return False
        time.sleep(0.05)
    return False
