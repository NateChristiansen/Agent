"""Drive one worker attempt, read its SDK stream, and stop its process group.
`supervisor.py` owns record writes and replaceable bindings; function-body imports
keep test monkeypatches visible.
"""

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
from claude_agent_sdk.types import TERMINAL_TASK_STATUSES

from .. import launcher
from ..append_log import open_append
from ..core import AgentError, is_alive, iso, now
from . import notice_producers, streams
from . import state as state_mod
from . import usage as usage_mod
from .isolated_transport import IsolatedClaudeTransport

# SDK-to-dictionary conversion lives in normalize.py.
from .normalize import out_record, result_envelope, return_status
from .state import CANCEL_GRACE_SECONDS, file, load, save

# POLL_SECONDS, QUESTION_IDLE_SECONDS and WATCHDOG_SECONDS are read as `state_mod.X` at call
# time rather than imported by name: the tests turn all three down, and a `from .state import`
# binding would be a copy this module kept reading past the patch. (The alias keeps the local
# `state` dicts in the loop below unshadowed.)


def detach(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    """Start the supervisor in its own session (POSIX) or process group (Windows); on Windows
    it tries job breakaway and records the result."""
    from . import verification_shell
    from .env_names import claude_named
    from .supervisor import popen

    err_path = file(root, d["id"], ".err")
    # No CLAUDE* var: that is what keeps a supervisor from carrying its manager's session id, and
    # so from being selected by the reapers of that session. It carries its own dispatch id
    # instead, so a supervisor stuck after its dispatch
    # ended is still findable. The reapers never select a supervisor either way.
    # Dropping CLAUDE_CODE_GIT_BASH_PATH too, the verifier's Git Bash is resolved here instead.
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
    # resolved in the body, not as a default: a default argument binds the constant at import
    # time, so the module value could be changed and this would keep the old one
    idle_seconds: float | None = None,
) -> dict[str, Any]:
    """Run one attempt to a stop, and report what stopped it.

    Returns `{"last_result", "exit_code", "timed_out", "stop_reason"}`. `evaluate` reads
    `last_result` off the record and decides from there. The connection is held open for the
    whole attempt, so a `question` is answered in the same session rather than by a `--resume`
    relaunch. A worker once burned three attempts on exactly that relaunch.

    `dispatch run` is started by `detach` in its own session or process group, apart from the
    manager's. The isolated transport gives each CLI child its own process group
    for cleanup while the trusted host retains notification access.
    """
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
    """The supervisor loop: one reader over `receive_messages()`, one poller over the inbox,
    the cancel flag and the deadlines, and a `stop` event either can set.

    The reader NEVER counts results. A steer sent with `client.query()` while a turn is open
    folds into that turn, so "probe + steer" produces one result, not two. The spike's loop
    waited for a second one and sat idle for 55 minutes. What ends the attempt is a result
    whose `structured_output.status` is not `question`: done, blocked, or an error subtype
    such as `error_max_budget_usd`, once its known delegated tasks have finished.
    """
    from .supervisor import remember_worker_pid, transport_factory

    out_path, err_path = file(root, d["id"], ".out"), file(root, d["id"], ".err")
    state: dict[str, Any] = {
        "last_result": None,
        "session_established": False,
        "timed_out": False,
        "stop_reason": None,
        # live token count and when it was last written to the record
        **usage_mod.initial_state(d),
    }
    stop = asyncio.Event()
    # line-buffered: a block-buffered handle held open for a 45-minute worker shows nothing
    # until the process exits, which hid the spike's hang for 45 minutes
    with (
        out_path.open("w", encoding="utf-8", buffering=1) as out,
        err_path.open("a", encoding="utf-8", buffering=1) as err,
    ):
        note(
            err,
            f"launch: {options.cli_path} in {options.cwd} ({'resume' if options.resume else 'session'} {d['session_id']})",
        )
        client: ClaudeSDKClient | None = None
        transport: Any = None
        startup_stage = "transport construction"
        try:
            with warnings.catch_warnings():
                # The SDK warns at connect that whole-tool allow entries (`Edit`, `Write`,
                # `WebFetch`, `mcp__agent__log`, ...) auto-approve before `can_use_tool` runs.
                # The permission rules stay the primary mechanism and
                # the callback is the recorder and backstop for what falls through them.
                # Gating every call would mean a PreToolUse hook, which is a different feature
                # with a per-call cost on the hot path.
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
        except Exception as exc:  # noqa: BLE001 — the supervisor reports, it never crashes
            stage = (
                getattr(transport, "startup_stage", startup_stage)
                if startup_stage == "connect"
                else startup_stage
            )
            note(err, f"transport error during {stage}: {type(exc).__name__}: {exc!r}")
            state["stop_reason"] = state["stop_reason"] or "error"
        finally:
            # Bounded on purpose. The SDK's own teardown bounds every await (~20s worst
            # case), but a custom transport's does not have to, and a supervisor wedged in
            # cleanup is the failure this whole rewrite exists to stop.
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(), state_mod.WATCHDOG_SECONDS)
                except Exception as exc:  # noqa: BLE001 — cleanup failure must prevent acceptance
                    note(err, f"cleanup failed: {exc}")
                    state["stop_reason"] = "cleanup_failed"
        note(err, f"worker stopped: {state['stop_reason']}")
    # A closed stream or an interrupt cannot turn unfinished delegation into a usable return.
    result = (
        None if state.get("pending_children") or state.get("children_timed_out") else state["last_result"]
    )
    return {
        "last_result": result,
        "session_established": bool(state["session_established"]),
        # There is no process exit code any more. `evaluate`'s table still keys on one for the
        # "it ended badly with nothing usable" branch, so a result that is not an error reads
        # as 0 and everything else as 1. Those are the same two cases the exit code ever
        # distinguished (a budget hit exits 1 too, which is why `_hit_budget` reads the
        # envelope instead).
        "exit_code": 0
        if result is not None and not result.get("is_error") and state["stop_reason"] != "cleanup_failed"
        else 1,
        "timed_out": bool(state["timed_out"]),
        "stop_reason": state["stop_reason"],
        "estimated_cost_usd": float(state.get("estimated_cost_usd") or 0.0),
    }


def _persist_resolved_model(root: Path, dispatch_id: str, model: str) -> None:
    """The model id the worker actually ran, from its first reply. A row names
    an alias; when the vendor moves the alias, this is where the new id first shows, and
    doctor and the handoff flag one the rubric has no recorded default for."""
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
    """Mirror the stream; a non-question result ends the attempt after known children finish."""
    from .supervisor import persist_result

    try:
        async for msg in client.receive_messages():
            record = out_record(msg)
            out.write(json.dumps(record, default=str) + "\n")
            if isinstance(msg, (SystemMessage, AssistantMessage, ResultMessage)):
                # A pinned session id is not proof the CLI created it. A run that failed in
                # the version probe was retried with --resume for a missing id.
                state["session_established"] = True
            if isinstance(msg, SystemMessage):
                _track_child(record, state)
                if (
                    state.get("pending_children")
                    or state.get("children_timed_out")
                    or not state.get("provisional_result")
                ):
                    continue
                envelope = state.pop("provisional_result")
            elif not isinstance(msg, ResultMessage):
                if isinstance(msg, AssistantMessage):
                    state["answer_wait_until"] = None
                    if state.get("question_deadline") is not None:
                        # Activity after a question shows the queued answer is being processed.
                        # Clear the question hold and restart result timing for the new turn.
                        state["question_deadline"] = None
                        state["first_result_at"] = None
                        note(err, "worker is working again after its question; hold cleared")
                        await asyncio.to_thread(notice_producers.answered, root, str(d["id"]))
                    usage_mod.count_message(root, str(d["id"]), msg, state)
                    if msg.model and not state.get("resolved_model"):
                        state["resolved_model"] = str(msg.model)
                        _persist_resolved_model(root, str(d["id"]), str(msg.model))
                continue
            else:
                envelope = result_envelope(msg)
                if _is_queue_drain(envelope):
                    # On resume, an empty result drains queued task notifications before the
                    # new message is processed. It is not the worker's return.
                    out.flush()
                    note(err, "empty result (queued notifications drained); waiting for the worker's return")
                    continue
            if state.get("children_timed_out"):
                continue
            if state.get("pending_children"):
                # Questions are provisional too; the owner notice waits until delegation finishes.
                state["provisional_result"] = envelope
                state["last_result"] = None
                state["first_result_at"] = time.monotonic()
                note(err, "worker result is provisional; waiting for delegated tasks")
                continue
            state["last_result"] = envelope
            # persisted per result, not once at the end, so `collect` can still finalize a
            # dispatch whose supervisor was killed after the worker had already returned
            persist_result(root, d["id"], envelope)
            # the clock the watchdog runs from: the LATEST result, not the first. A worker
            # that answers a question after a steer is mid-turn again, and the steer resets
            # this to None (see poll_worker). The next result starts it afresh.
            state["first_result_at"] = time.monotonic()
            status = return_status(envelope)
            answered_early, state["answer_pending"] = bool(state.get("answer_pending")), False
            if status == "question" and answered_early:
                # An answer already delivered before the question result needs no question hold.
                # Require progress within the idle window instead.
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
            # `or` because an interrupt produces a result too: on a timeout or a cancel the
            # poller has already said why we are stopping, and that reason is the true one
            state["stop_reason"] = state["stop_reason"] or "result"
            break
    finally:
        usage_mod.count(root, str(d["id"]), None, state, force=True)
        state["stop_reason"] = state["stop_reason"] or "stream-end"
        stop.set()


def _track_child(record: dict[str, Any], state: dict[str, Any]) -> None:
    task_id = record.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return
    pending: set[str] = state.setdefault("pending_children", set())
    if record.get("subtype") == "task_started":
        if record.get("task_type") in (
            "local_agent",
            "remote_agent",
            "in_process_teammate",
            "local_workflow",
        ):
            pending.add(task_id)
    elif (
        record.get("subtype") in ("task_notification", "task_updated")
        and record.get("status") in TERMINAL_TASK_STATUSES
    ):
        pending.discard(task_id)


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
    """Everything the reader cannot see: a steer waiting in the inbox, a cancel written to the
    record by another process, the attempt's own deadline, and the watchdog."""
    from ..vault import live

    deadline = time.monotonic() + timeout
    beat = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(state_mod.POLL_SECONDS)
        clock = time.monotonic()
        if clock - beat >= live.HEARTBEAT_SECONDS:
            # The Dashboard's live view reads last activity and last tool from this snapshot;
            # off the event loop, so a slow disk never delays a cancel or a steer.
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
            # Completion or another result can arrive during interrupt. Neither can accept
            # a return whose children were still running when the attempt clock expired.
            state["children_timed_out"] = bool(state.get("pending_children"))
            await _interrupt(client, err)
            return _stop(state, stop, "timeout")
        message = take_inbox(inbox)
        if message:
            note(err, f"steer delivered ({len(message)} chars)")
            if asked := state.get("question_deadline") is not None:  # before sending, or a quick
                await asyncio.to_thread(notice_producers.answered, root, str(d["id"]))  # return drops it
            await client.query(message)  # lands inside the running turn (see ADR-0009)
            if not asked and state.get("question_deadline") is not None:  # asked as it was sent
                await asyncio.to_thread(notice_producers.answered, root, str(d["id"]))
            elif not asked:
                state["answer_pending"] = True  # an answer ahead of its question result
            state["question_deadline"] = None
            # The worker is working again, for as long as the answer takes. Only the
            # attempt's own timeout bounds that. Without this reset the watchdog fired
            # WATCHDOG_SECONDS after the question and cut the answer short.
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
    """A success result with no structured output, no error and zero turns is the CLI
    draining queued notifications on a resume, not a return (see read_worker)."""
    return (
        envelope.get("structured_output") is None
        and not envelope.get("is_error")
        and int(envelope.get("num_turns") or 0) == 0
        and str(envelope.get("subtype") or "") == "success"
    )


def _watchdog_deadline(state: dict[str, Any]) -> float | None:
    """`WATCHDOG_SECONDS` after the worker's latest result, or after a `question` hold expires
    when one is open. A hold is a deliberate wait, a hang is not, and the watchdog must not
    cut the first one short. `None` until a result has arrived, and `None` again after a
    steer (the worker is mid-turn and only the attempt's timeout bounds it): a steered turn
    always ends in a result under the SDK, so nothing here has to guess how long an answer
    takes. The first migration counted from the FIRST result and would have stopped every
    real answer 30 seconds in."""
    if state.get("pending_children"):
        return None  # Delegation is bounded by the attempt clock, not the post-result watchdog.
    first = state.get("first_result_at")
    if first is None:
        return None
    held = state.get("question_deadline")
    return max(first, held or first) + state_mod.WATCHDOG_SECONDS


def _stop(state: dict[str, Any], stop: asyncio.Event, reason: str) -> None:
    state["stop_reason"] = reason
    stop.set()


async def _interrupt(client: ClaudeSDKClient, err: Any) -> None:
    """Interrupt best effort within a deadline. A worker that already finished may refuse the
    interrupt without indicating a failure.
    """
    try:
        await asyncio.wait_for(client.interrupt(), CANCEL_GRACE_SECONDS)
    except Exception as exc:  # noqa: BLE001 — never let cleanup take the supervisor down
        note(err, f"interrupt was refused or timed out: {exc}")


def cancel_requested(root: Path, dispatch_id: str) -> bool:
    """`cancel` runs in another process and writes `cancelled` to the record. That write is
    how the supervisor learns of it now that the worker is a connection we own rather than a
    child process someone else can signal."""
    try:
        return load(root, dispatch_id).get("state") == "cancelled"
    except AgentError, OSError:
        return False


def note(err: Any, text: str) -> None:
    err.write(f"--- {iso(now())} {text}\n")


def take_inbox(inbox: Path | None) -> str:
    """Read and clear the steer inbox. Truncating rather than deleting keeps `dispatch say`
    able to write to a path that always exists."""
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
    """Signal the process group claude leads (start_new_session made it the leader). A group
    that is already gone, or holds only zombies waiting for the supervisor to reap them,
    raises ProcessLookupError or PermissionError on macOS. Both mean nothing left to kill."""
    if sys.platform == "win32":
        return False
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError, PermissionError:
        return False
    return True


def kill_group(pid: int, *, root: Path | None = None, dispatch_id: str = "") -> list[int]:
    """From another process (cancel's fallback): SIGTERM the group, wait up to the grace
    period for the supervisor to reap it, then SIGKILL."""
    if sys.platform == "win32":
        from .. import procs

        # The leader may already be gone. Every surviving child inherits the dispatch tag;
        # reap verifies each target's birth time before killing its exact PID.
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
    """Wait for the supervisor to end the attempt it was told to cancel. `stop_reason` is
    cleared when an attempt starts and written when `run_worker` returns, so it is the one
    field that says "this attempt is over" without depending on a pid the fake transport (or
    a future SDK) may not have. False means the supervisor did not stop within the grace
    period, or is not there at all. That is the only case `cancel` reaches for a kill."""
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while time.monotonic() < deadline:
        d = load(root, dispatch_id)
        if d.get("stop_reason") is not None:
            return True
        if not is_alive(d.get("supervisor_pid")):
            return False
        time.sleep(0.05)
    return False
