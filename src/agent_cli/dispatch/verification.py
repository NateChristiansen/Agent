"""Bounded supervisor-owned verification, not yet connected to dispatch finalization.

G09 must require both validated evidence and cleanup_confirmed, and hold a lease using
requires_lease even after the model worker/supervisor dies. Files are not authenticated
against a same-UID attacker. Command output is capped, not redacted; explicitly granted
credentials or secrets read from disk can still be printed by an authorized command.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..core import AgentError
from ..repos import VerificationConfig, verification_config
from . import verification_process as procs
from . import verification_status
from .verification_evidence import Binding, CommandOutcome, Evidence, config_digest
from .verification_status import RunStatus, load_status
from .verification_store import ERROR_MESSAGE_LIMIT, Store

LOG_LIMIT = 65536
#: Cap on one tree observation, all of its Git queries together, inside the run's own deadline.
OBSERVE_SECONDS = 30


observe_tree = procs.observe_tree


@dataclass(frozen=True)
class Prepared:
    root: Path
    worktree: Path
    binding: Binding
    config: VerificationConfig
    environment: dict[str, str] = field(repr=False)
    launch_base: str | None = None
    tools: dict[str, str] = field(default_factory=dict[str, str])


@dataclass
class Handle:
    root: Path
    binding: Binding
    process: subprocess.Popen[bytes]
    timeout_seconds: float


_cleanup_record = verification_status.cleanup_record


def prepare(
    root: Path,
    worktree: Path,
    *,
    dispatch_id: str,
    attempt: int,
    final_sha: str,
    config: VerificationConfig,
    environment: dict[str, str] | None = None,
    launch_base: str | None = None,
    telemetry_sink: Path | None = None,
    telemetry_parent: str | None = None,
    run_id: str | None = None,
) -> Prepared:
    """Persist without execution. G09 records binding under its dispatch guard before launch.

    `telemetry_sink` and `telemetry_parent` route the verifier's own pytest events into the main
    sink under the supervisor's verification or reverify event."""
    if os.name not in {"posix", "nt"}:
        raise AgentError("verification runner requires POSIX process groups or Windows jobs")
    tools = procs.platform_tools()
    worktree = worktree.resolve(strict=True)
    if not worktree.is_dir():
        raise ValueError("verification worktree must be a directory")
    binding = Binding(dispatch_id, attempt, final_sha, config_digest(config), run_id or secrets.token_hex(16))
    env = dict(procs.BASE_ENV if environment is None else environment)
    if any(not k or "=" in k or "\0" in k + v for k, v in env.items()):
        raise ValueError("verification environment must contain valid explicit string entries")
    env.update(AGENT_ROOT=str(root.resolve()), AGENT_DISPATCH_ID=dispatch_id)
    env["AGENT_VERIFY_RUN_ID"] = binding.run_id
    if telemetry_sink is not None and telemetry_parent:
        env.update(AGENT_TELEMETRY_SINK=str(telemetry_sink), AGENT_TELEMETRY_PARENT=telemetry_parent)
    store = Store(root, binding, create=True)
    life = {
        "phase": "created",
        "creator_pid": os.getpid(),
        "guardian_pid": None,
        "command_pid": None,
        "watchdog_pid": None,
        "command_index": None,
        "cleanup_confirmed": False,
        "finished": False,
        "reason": "pending",
        "started": time.monotonic(),
    }
    try:
        store.evidence(Evidence(1, binding, "running", None, None, (), 0))
        store.lifecycle(life)
        return Prepared(
            root.resolve(), worktree.resolve(strict=True), binding, config, env, launch_base, tools
        )
    finally:
        store.close()


def launch_prepared(prepared: Prepared) -> Handle:
    store = Store(prepared.root, prepared.binding)
    process = None
    try:
        with store.lock():
            life = store.load_lifecycle()
            if life["phase"] != "created" or life["guardian_pid"] is not None or _cancelled(store):
                raise AgentError("prepared verification is cancelled, launched, or no longer current")
            if config_digest(prepared.config) != prepared.binding.config_digest:
                raise ValueError("prepared verifier configuration changed")
            life = {k: v for k, v in life.items() if k not in {"binding", "schema_version"}}
            life["started"] = time.monotonic()
            process = procs.launch("guardian", env=prepared.environment, cwd=prepared.root)
            life["guardian_pid"] = process.pid
            store.lifecycle(life)
            procs.send(
                process,
                {
                    "root": str(prepared.root),
                    "worktree": str(prepared.worktree),
                    "binding": asdict(prepared.binding),
                    "config": asdict(prepared.config),
                    "environment": prepared.environment,
                    "launch_base": prepared.launch_base,
                    **prepared.tools,
                },
            )
        return Handle(prepared.root, prepared.binding, process, prepared.config.timeout_seconds)
    except BaseException:
        if process is not None:
            procs.close_control(process)
            process.wait(timeout=5)
        raise
    finally:
        store.close()


def start(root: Path, worktree: Path, **kwargs: Any) -> Handle:
    """Convenience for isolated callers; G09 uses prepare, publish binding, launch_prepared."""
    return launch_prepared(prepare(root, worktree, **kwargs))


def request_cancel(root: Path, binding: Binding) -> None:
    store = Store(root, binding)
    try:
        try:
            store.write("cancel.json", {"binding": asdict(binding)}, immutable=True)
        except FileExistsError:
            if store.read("cancel.json") != {"binding": asdict(binding)}:
                raise ValueError("stale verification cancellation") from None
    finally:
        store.close()


def recover(root: Path, binding: Binding) -> RunStatus:
    """Record interruption after guardian death; never rerun or manufacture success."""
    store = Store(root, binding)
    try:
        with store.lock():
            status = load_status(root, binding)
            cancelled_preparation = (
                status.lifecycle["phase"] == "created"
                and status.lifecycle["guardian_pid"] is None
                and _cancelled(store)
            )
            if status.active and not cancelled_preparation:
                return status
            if store.read("terminal.json") is None:
                prior = status.evidence
                store.evidence(
                    Evidence(
                        1,
                        binding,
                        "cancelled" if cancelled_preparation else "interrupted",
                        prior.before if prior else None,
                        None,
                        prior.commands if prior else (),
                        max(0, time.monotonic() - status.lifecycle["started"]),
                    )
                )
                life = {k: v for k, v in status.lifecycle.items() if k not in {"schema_version", "binding"}}
                cleaned = status.cleanup_confirmed or (life["guardian_pid"] is None and not status.active)
                if cancelled_preparation:
                    cleaned = True
                life.update(
                    phase="finished",
                    finished=True,
                    cleanup_confirmed=cleaned,
                    reason="cancelled" if cancelled_preparation else "guardian_interrupted",
                )
                store.lifecycle(life)
            elif status.cleanup_confirmed and not status.lifecycle["cleanup_confirmed"]:
                life = {k: v for k, v in status.lifecycle.items() if k not in {"schema_version", "binding"}}
                life["cleanup_confirmed"] = True
                store.lifecycle(life)
    finally:
        store.close()
    return load_status(root, binding)


def wait(handle: Handle) -> RunStatus:
    try:
        handle.process.wait(timeout=handle.timeout_seconds + 15)
    except subprocess.TimeoutExpired:
        request_cancel(handle.root, handle.binding)
        procs.close_control(handle.process)
        handle.process.kill()
        handle.process.wait(timeout=3)
    finally:
        procs.close_control(handle.process)
    deadline = time.monotonic() + procs.CLEANUP_SECONDS + 3
    while time.monotonic() < deadline:
        status = load_status(handle.root, handle.binding)
        if status.cleanup_confirmed:
            break
        time.sleep(procs.POLL)
    return recover(handle.root, handle.binding)


def run(root: Path, worktree: Path, **kwargs: Any) -> RunStatus:
    return wait(start(root, worktree, **kwargs))


def _cancelled(store: Store) -> bool:
    raw = store.read("cancel.json")
    if raw is None:
        return False
    if raw != {"binding": asdict(store.binding)}:
        raise ValueError("invalid verifier cancellation binding")
    return True


def _exit_outcome(deadline: float, returncode: int) -> tuple[Literal["exited", "timeout"], int | None]:
    """The wait loop only checks the deadline at its top, so a watchdog kill that lands while
    the loop sits in select() comes back on the next line as an ordinary child exit. Past the
    deadline the watchdog is the only thing that ended this child, so call it what it was: on a
    loaded machine the command was being recorded `exited` with a signal code instead of
    `timeout`.
    """
    if time.monotonic() >= deadline:
        return "timeout", None
    return "exited", returncode


def _drain(stream: Any, log: Any, deadline: float) -> None:
    """Keep what an exited command left in its pipe. The read loop can see the exit before the
    last output: the command writes and exits between `select()` and `poll()`, which a loaded
    machine makes common, and `/usr/bin/env` output went missing from output.log.
    Everything the command itself wrote is already buffered once it has exited, so this reads to
    EOF or until nothing more is waiting. It never waits for more: a descendant that still holds
    the pipe open cannot stall the runner. LOG_LIMIT and the command's deadline still bound it."""
    fd = stream.fileno()
    while log.tell() < LOG_LIMIT and time.monotonic() < deadline:
        if not procs.readable(fd, 0):
            return
        chunk = os.read(fd, 8192)
        if not chunk:
            return
        log.write(chunk[: LOG_LIMIT - log.tell()])
        log.flush()


def _close_control(process: subprocess.Popen[bytes]) -> None:
    """A send that failed on a broken pipe, such as the watchdog ending the job while the
    command's request waited, leaves that request buffered, and closing flushes it into the
    same pipe and raises again. The handle is closed either way; the error must not skip the
    rest of cleanup."""
    try:
        procs.close_control(process)
    except OSError:
        pass


def _await_watchdog(
    watchdog: subprocess.Popen[bytes], child: subprocess.Popen[bytes] | None, timeout: float
) -> None:
    """Wait for the watchdog, reaping the command as soon as its kill lands. Unreaped, the killed
    command is a zombie in its own group, which `killpg(pgid, 0)` reports as present (EPERM on
    macOS), so the watchdog could confirm cleanup only through `ps`, and a `ps` slower than its
    bound turned a plain timeout into unconfirmed cleanup."""
    deadline = time.monotonic() + timeout
    while watchdog.poll() is None:
        if child is not None:
            child.poll()
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(watchdog.args, timeout)
        time.sleep(procs.POLL)


def _command(
    store: Store,
    life: dict[str, Any],
    request: dict[str, Any],
    index: int,
    command: str,
    timeout: float,
    total_deadline: float,
    log: Any,
) -> CommandOutcome:
    started = time.monotonic()
    deadline = min(started + timeout, total_deadline)
    child = watchdog = None
    activated = False
    outcome: Literal["exited", "timeout", "cancelled", "error"] = "error"
    code: int | None = None
    try:
        life.update(
            phase="preparing",
            command_index=index,
            command_pid=None,
            watchdog_pid=None,
            cleanup_confirmed=False,
        )
        store.lifecycle(life)
        child = procs.launch(
            "command", env=request["environment"], cwd=Path(request["worktree"]), stdout=subprocess.PIPE
        )
        life["command_pid"] = child.pid
        store.lifecycle(life)
        # The watchdog starts before the command's request is sent: a request past the Windows
        # pipe buffer holds the send until the command has cold-started and read it, and the
        # watchdog's own cold start must overlap that wait, not follow it.
        watchdog = procs.launch("watchdog", env=procs.BASE_ENV, cwd=store.root, stdout=subprocess.PIPE)
        life["watchdog_pid"] = watchdog.pid
        procs.send(
            watchdog,
            {
                "root": str(store.root),
                "binding": asdict(store.binding),
                "index": index,
                "pgid": child.pid,
                "deadline": deadline,
            },
        )
        shell = {"shell": request["shell"]} if "shell" in request else {}
        procs.send(child, {"command": command, "env": request["environment"], **shell})
        assert watchdog.stdout is not None
        while not procs.readable(watchdog.stdout, procs.POLL):
            if time.monotonic() >= deadline or _cancelled(store) or procs.parent_gone():
                raise AgentError("verification interrupted before command activation")
        if os.read(watchdog.stdout.fileno(), 32) != b"ready\n" or time.monotonic() >= deadline:
            raise AgentError("verification watchdog did not become ready before deadline")
        life["phase"] = "running"
        store.lifecycle(life)
        assert child.stdin is not None
        child.stdin.write(b"go\n")
        child.stdin.flush()
        activated = True
        assert child.stdout is not None
        while True:
            if _cancelled(store):
                outcome = "cancelled"
                break
            if procs.parent_gone():
                life["reason"] = "parent_interrupted"
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                break
            if watchdog.poll() is not None:
                cleanup = _cleanup_record(store, life)
                outcome = "timeout" if cleanup and cleanup["reason"] == "timeout" else "error"
                break
            if procs.readable(child.stdout, procs.POLL):
                chunk = os.read(child.stdout.fileno(), 8192)
                if chunk and log.tell() < LOG_LIMIT:
                    log.write(chunk[: LOG_LIMIT - log.tell()])
                    log.flush()
            if child.poll() is not None:
                outcome, code = _exit_outcome(deadline, child.returncode)
                _drain(child.stdout, log, deadline)
                break
    except Exception:  # noqa: BLE001 — setup failure is an unsuccessful command outcome
        if not activated:
            message = b"\nverification interrupted before command activation\n"
            log.write(message[: max(0, LOG_LIMIT - log.tell())])
            log.flush()
        outcome, code = "error", None
    finally:
        life["phase"] = "cleanup"
        store.lifecycle(life)
        if child is not None:
            _close_control(child)
        cleaned = False
        if watchdog is not None:
            _close_control(watchdog)
            try:
                _await_watchdog(watchdog, child, procs.CLEANUP_SECONDS + 3)
            except subprocess.TimeoutExpired:
                watchdog.kill()
                watchdog.wait(timeout=2)
            if watchdog.stdout:
                watchdog.stdout.close()
            cleanup = _cleanup_record(store, life)
            cleaned = watchdog.returncode == 0 and bool(cleanup and cleanup["cleanup_confirmed"])
        if child is not None:
            if not cleaned:
                procs.kill_group(child.pid)  # Best effort; dead watchdog still cannot certify cleanup.
            child.wait(timeout=3)
            if child.stdout:
                child.stdout.close()
            procs.release(child.pid)
        life["cleanup_confirmed"] = cleaned
        if not cleaned:
            outcome, code = "error", None
            life["reason"] = "cleanup_unconfirmed"
        store.lifecycle(life)
    return CommandOutcome(index, outcome, code, time.monotonic() - started)


def guardian_main() -> None:
    request = procs.read_request()
    binding = Binding(**request["binding"])
    store = Store(Path(request["root"]), binding)
    life = {k: v for k, v in store.load_lifecycle().items() if k not in {"schema_version", "binding"}}
    config = verification_config(
        {
            "verify": request["config"]["commands"],
            "verify_timeout_seconds": request["config"]["timeout_seconds"],
        }
    )
    if config is None or config_digest(config) != binding.config_digest:
        raise ValueError("verification configuration binding mismatch")
    started = life["started"]
    deadline = started + config.timeout_seconds
    before = after = None
    results: list[CommandOutcome] = []
    state: Literal["interrupted", "cancelled", "complete"] = "interrupted"
    step = "lifecycle"
    try:
        life.update(phase="observing", reason="observing", cleanup_confirmed=True)
        store.lifecycle(life)
        step = "observe_tree"
        before = observe_tree(
            Path(request["worktree"]),
            timeout=min(OBSERVE_SECONDS, deadline - time.monotonic()),
            guardian_group=True,
            launch_base=request.get("launch_base"),
            git=request.get("git"),
        )
        step = "evidence"
        store.evidence(Evidence(1, binding, "running", before, None, (), time.monotonic() - started))
        step = "clean_check"
        if not before.clean_at(binding.final_sha):
            life["reason"] = "initial_tree_changed"
        else:
            step = "commands"
            with os.fdopen(procs.open_output_log(store), "wb") as log:
                for index, command in enumerate(config.commands):
                    if _cancelled(store) or procs.parent_gone():
                        state = "cancelled" if _cancelled(store) else "interrupted"
                        life["reason"] = "cancelled" if state == "cancelled" else "parent_interrupted"
                        break
                    result = _command(
                        store, life, request, index, command.command, command.timeout_seconds, deadline, log
                    )
                    results.append(result)
                    store.evidence(
                        Evidence(
                            1, binding, "running", before, None, tuple(results), time.monotonic() - started
                        )
                    )
                    if result.outcome != "exited" or result.exit_code != 0:
                        state = "cancelled" if result.outcome == "cancelled" else "interrupted"
                        if life["reason"] not in {"parent_interrupted", "cleanup_unconfirmed"}:
                            life["reason"] = (
                                "command_failed"
                                if result.outcome == "exited"
                                else "cancelled"
                                if result.outcome == "cancelled"
                                else "command_" + result.outcome
                            )
                        break
                else:
                    state = "complete"
                    life["reason"] = "commands_complete"
            step = "final_observe"
            after = observe_tree(
                Path(request["worktree"]),
                timeout=min(OBSERVE_SECONDS, deadline - time.monotonic()),
                guardian_group=True,
                launch_base=request.get("launch_base"),
                git=request.get("git"),
            )
    except procs.UnsupportedTree as exc:
        state = "interrupted"
        life["reason"] = "unsupported_tree"
        _record_error(life, step, exc)
    except Exception as exc:  # noqa: BLE001 — never persist raw exceptions/configured output as metadata
        state = "interrupted"
        if life["reason"] not in {"cleanup_unconfirmed", "parent_interrupted"}:
            life["reason"] = "runner_error"
        _record_error(life, step, exc)
    finally:
        try:
            if _cancelled(store):
                state = "cancelled"
            elif procs.parent_gone():
                state = "interrupted"
                life["reason"] = "parent_interrupted"
            store.evidence(
                Evidence(1, binding, state, before, after, tuple(results), max(0, time.monotonic() - started))
            )
        except Exception as exc:  # noqa: BLE001 — the lifecycle must still be written
            if "error" not in life:
                if life["reason"] not in {"cleanup_unconfirmed", "parent_interrupted"}:
                    life["reason"] = "runner_error"
                _record_error(life, "terminal_evidence", exc)
        try:
            life.update(phase="finished", finished=True)
            store.lifecycle(life)
        finally:
            store.close()


def _record_error(life: dict[str, Any], step: str, exc: Exception) -> None:
    # An AgentError's message is a fixed string from src; any other text may carry output.
    message = exc.message[:ERROR_MESSAGE_LIMIT] if isinstance(exc, AgentError) else None
    life["error"] = {"step": step, "type": type(exc).__name__[:100], "message": message}
