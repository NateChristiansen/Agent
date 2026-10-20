"""Explicit environments at the Claude CLI spawn boundary (see ADR-0019).

The SDK host is still the trusted supervisor and retains its notification environment.
Only the version probe, CLI and inheriting descendants receive the restricted environment.
This small launch adapter reuses the audited SDK's framing, protocol I/O and close logic.
"""

# Launch/stream setup adapted from claude-agent-sdk-python 0.2.152,
# src/claude_agent_sdk/_internal/transport/subprocess_cli.py.
# https://github.com/anthropics/claude-agent-sdk-python
# MIT License
#
# Copyright (c) 2025 Anthropic, PBC
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import contextlib
import inspect
import os
import re
import signal
import sys
from importlib.metadata import version
from subprocess import PIPE
from typing import Any

import anyio
from anyio.abc import Process
from anyio.streams.text import TextReceiveStream, TextSendStream
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal._task_compat import spawn_detached
from claude_agent_sdk._internal.transport import subprocess_cli as sdk_transport
from claude_agent_sdk.types import (
    _configure_can_use_tool,  # pyright: ignore[reportPrivateUsage]  # claude_agent_sdk private
)

from ..core import AgentError

AUDITED_SDK_VERSION = "0.2.152"
SDK_VERSION = version("claude-agent-sdk")
# A newly installed macOS CLI can spend two seconds in Gatekeeper before it prints
# anything, which the old two-second limit did not allow for. This probe is
# bounded separately from the worker's own execution timeout.
VERSION_PROBE_TIMEOUT_SECONDS = 30


def check_compatibility() -> None:
    """Never fall back to the SDK's ambient-environment launch after dependency drift."""
    cls = sdk_transport.SubprocessCLITransport
    methods = ("_build_command", "_handle_stderr", "write", "read_messages", "close")
    compatible = (
        SDK_VERSION == AUDITED_SDK_VERSION
        and tuple(inspect.signature(cls.__init__).parameters) == ("self", "prompt", "options")
        and all(callable(getattr(cls, method, None)) for method in methods)
        and isinstance(getattr(sdk_transport, "_ACTIVE_CHILDREN", None), set)
        and callable(getattr(sdk_transport, "spawn_detached", None))
    )
    if not compatible:
        raise AgentError(
            f"Claude isolation adapter supports audited claude-agent-sdk {AUDITED_SDK_VERSION}; "
            f"installed {SDK_VERSION} has not passed its compatibility gate. "
            "Use the audited dependency or review the adapter and run the real environment-boundary tests. "
            "No ambient-environment fallback is permitted."
        )


def child_environment(options: ClaudeAgentOptions) -> dict[str, str]:
    """Effective grant names/values; no ambient SDK, tracing or session state is inherited."""
    env = dict(options.env or {})
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        if key in env:
            raise AgentError(f"{key} is reserved manager identity and cannot enter a worker environment")
    env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    env["CLAUDE_AGENT_SDK_VERSION"] = SDK_VERSION
    if options.enable_file_checkpointing:
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"
    if options.cwd:
        env["PWD"] = str(options.cwd)
    return env


def _kill_group(process: Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


async def _group_has_no_live_members(pgid: int) -> bool:
    """Bounded POSIX observation: process-group ID and state only, never argv or env."""
    try:
        with anyio.fail_after(1):
            result = await anyio.run_process(
                ["/bin/ps", "-axo", "pgid=,stat="], env={"LC_ALL": "C"}, check=False
            )
        if result.returncode != 0:
            return False
        for line in result.stdout.decode("ascii").splitlines():
            fields = line.split()
            if len(fields) != 2 or not fields[0].isdigit():
                return False
            if int(fields[0]) == pgid and not fields[1].startswith("Z"):
                return False
        return True
    except OSError, TimeoutError, UnicodeError:
        return False


def _windows_job(process: Process) -> Any:
    if sys.platform != "win32":
        return None
    from ..win_tree import Job

    try:
        return Job(process.pid)
    except OSError:
        return None


async def _reap(process: Process, job: Any = None, *, require_tree: bool = True) -> None:
    if sys.platform == "win32":
        from ..win_tree import kill_tree

        confirmed = False
        try:
            if job is not None:
                try:
                    job.terminate()
                    confirmed = True
                except OSError:
                    pass
            if not confirmed:
                confirmed = kill_tree(process.pid)
        finally:
            if job is not None:
                job.close()
        with anyio.move_on_after(2, shield=True):
            await process.wait()
        if process.returncode is None or (require_tree and not confirmed):
            raise AgentError("Claude process-tree cleanup could not be confirmed")
        sdk_transport._ACTIVE_CHILDREN.discard(process)  # pyright: ignore[reportPrivateUsage]  # claude_agent_sdk private
        return
    try:
        _kill_group(process)
    except PermissionError:
        # macOS may report EPERM for an absent/zombie group. Leader exit alone is
        # insufficient: a denied group with any live descendant remains an error.
        with anyio.move_on_after(2, shield=True):
            await process.wait()
        if process.returncode is None or not await _group_has_no_live_members(process.pid):
            raise
    with anyio.move_on_after(2, shield=True):
        await process.wait()
    if process.returncode is None:
        raise AgentError("Claude process cleanup timed out; child exit is unconfirmed")
    sdk_transport._ACTIVE_CHILDREN.discard(process)  # pyright: ignore[reportPrivateUsage]  # claude_agent_sdk private


class IsolatedClaudeTransport(sdk_transport.SubprocessCLITransport):
    """SDK 0.2.152 transport with locally owned version/main spawn and group cleanup."""

    def __init__(self, options: ClaudeAgentOptions):
        check_compatibility()
        if options.session_store is not None:
            raise AgentError("Isolated Claude transport does not support session_store materialization")
        if not options.cli_path or not os.path.isabs(str(options.cli_path)):
            raise AgentError(
                "Isolated Claude transport requires the resolved absolute PATH Claude executable"
            )
        # The SDK normally makes this options copy while constructing its own transport.
        # A custom transport bypasses that construction, so preserve stdio permission routing.
        configured = _configure_can_use_tool(options)
        super().__init__(prompt="", options=configured)
        self._child_env = child_environment(configured)
        self._job: Any = None
        self.startup_stage = "version probe"

    async def _version_probe(self) -> None:
        process: Process | None = None
        job: Any = None
        try:
            with anyio.fail_after(VERSION_PROBE_TIMEOUT_SECONDS):
                process = await anyio.open_process(
                    [str(self._cli_path), "-v"],
                    stdin=PIPE,
                    stdout=PIPE,
                    stderr=PIPE,
                    cwd=self._cwd,
                    env=dict(self._child_env),
                    user=self._options.user,
                    start_new_session=sys.platform != "win32",
                )
                job = _windows_job(process)
                sdk_transport._ACTIVE_CHILDREN.add(process)  # pyright: ignore[reportPrivateUsage]  # claude_agent_sdk private
                if process.stdout:
                    output = (await process.stdout.receive()).decode(errors="replace").strip()
                    match = re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", output)
                    if not match:
                        raise AgentError("Claude CLI version probe returned unrecognized output")
                    if tuple(map(int, match[1].split("."))) < (2, 0, 0):
                        raise AgentError("Claude CLI is older than the audited SDK minimum 2.0.0")
        except TimeoutError as exc:
            raise AgentError(
                f"Claude CLI version probe timed out after {VERSION_PROBE_TIMEOUT_SECONDS}s "
                f"while starting {self._cli_path}"
            ) from exc
        finally:
            if process is not None:
                await _reap(process, job, require_tree=False)
                with anyio.move_on_after(2, shield=True):
                    await process.aclose()

    async def connect(self) -> None:
        if self._process is not None:
            return
        # Both subprocesses are created with explicit env mappings. Upstream connect and its
        # ambient version probe are deliberately never invoked.
        self.startup_stage = "version probe"
        await self._version_probe()
        try:
            self.startup_stage = "CLI spawn"
            self._process = await anyio.open_process(
                self._build_command(),
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE if self._options.stderr is not None else None,
                cwd=self._cwd,
                env=dict(self._child_env),
                user=self._options.user,
                start_new_session=sys.platform != "win32",
            )
            self._job = _windows_job(self._process)
            sdk_transport._ACTIVE_CHILDREN.add(self._process)  # pyright: ignore[reportPrivateUsage]  # claude_agent_sdk private
            if self._process.stdout:
                self._stdout_stream = TextReceiveStream(self._process.stdout)
            if self._process.stderr:
                self._stderr_stream = TextReceiveStream(self._process.stderr)
                self._stderr_task = spawn_detached(self._handle_stderr())
            if self._process.stdin:
                self._stdin_stream = TextSendStream(self._process.stdin)
            self._ready = True
            self.startup_stage = "SDK initialize"
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        process = self._process
        try:
            await super().close()
        finally:
            # The leader may already have exited while descendants still hold pipes open.
            # Synchronous group kill also runs under a raw asyncio cancellation.
            if process is not None:
                await _reap(process, self._job)
                self._job = None
