"""Stdio JSON-RPC connection to the Codex app-server."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .. import procs
from ..codex_executable import child_env
from ..codex_executable import command as codex_command
from ..core import AgentError
from ..records.narrow import as_dict_or_none

RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# One JSON-RPC message is one line. A resumed thread's history arrives as a single reply, far past
# asyncio's 64 KiB default; 64 MiB keeps a bound.
FRAME_LIMIT = 64 * 1024 * 1024
# The app-server's stderr was discarded, which left transport failures with no diagnostic. Its tail
# is kept in memory, bounded, and written only when a caller reports a failure.
STDERR_TAIL = 8 * 1024


class CodexAppServer:
    """One app-server process, with concurrent replies and notifications."""

    def __init__(
        self,
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
        command: tuple[str, ...] | None = None,
        handle_request: RequestHandler | None = None,
        frame_limit: int = FRAME_LIMIT,
    ) -> None:
        self.cwd = cwd
        self.frame_limit = frame_limit
        # Set when the reader stops on a protocol failure; pending calls and the closing event carry it.
        self.failure: AgentError | None = None
        self._closed_error: AgentError | None = None
        self.env = env
        self.command = command if command is not None else tuple(codex_command("app-server", "--stdio"))
        self.handle_request = handle_request
        self.process: asyncio.subprocess.Process | None = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._methods: dict[int, str] = {}
        self._next_id = 1
        self._reader: asyncio.Task[None] | None = None
        self._stderr = bytearray()
        self._stderr_reader: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._request_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> CodexAppServer:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.cwd),
                env=child_env(self.env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=self.frame_limit,
            )
        except OSError as exc:
            raise AgentError(f"Codex app-server could not start: {exc}") from exc
        self._reader = asyncio.create_task(self._read())
        self._stderr_reader = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(
                self.call(
                    "initialize",
                    {"clientInfo": {"name": "agent-dispatch", "title": "Agent dispatch", "version": "1"}},
                ),
                timeout=15,
            )
            await self.send("initialized", {})
        except BaseException:
            # `async with` never calls __aexit__ for a failed __aenter__; the child is ours to reap.
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        for task in tuple(self._request_tasks):
            task.cancel()
        for task in tuple(self._request_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        process = self.process
        if process is not None and process.returncode is None:
            await asyncio.to_thread(procs.kill_group, process.pid)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        for reader in (self._reader, self._stderr_reader):
            if reader is not None:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
        if process is not None:
            # Close the pipe transports while the loop still runs. Left to the garbage collector,
            # Windows' Proactor transports are finalized after `asyncio.run` closed the loop and
            # print "ValueError: I/O operation on closed pipe" tracebacks, three per doctor run
            # from Git Bash. `_transport` has no public close.
            if process.stdin is not None:
                process.stdin.close()
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
            await asyncio.sleep(0)  # let the close callbacks run before the loop ends

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if error := self.failure or self._closed_error:
            raise error
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._methods[request_id] = method
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            message = await asyncio.wait_for(future, 30)
        finally:
            self._pending.pop(request_id, None)
            self._methods.pop(request_id, None)
        if "error" in message:
            error = message["error"]
            fields = as_dict_or_none(error)
            detail = fields.get("message", str(error)) if fields is not None else str(error)
            raise AgentError(f"Codex {method}: {detail}")
        result = as_dict_or_none(message.get("result"))
        if result is None:
            raise AgentError(f"Codex {method}: missing result object")
        return result

    async def send(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise AgentError("Codex app-server is not running")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            if error := self.failure or self._closed_error:
                raise error
            process.stdin.write(data)
            await process.stdin.drain()

    async def _read(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            while line := await self._frame(process.stdout):
                try:
                    message = as_dict_or_none(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if message is None:
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int) and (future := self._pending.get(request_id)):
                    if not future.done():
                        future.set_result(message)
                elif request_id is not None and "method" in message:
                    task = asyncio.create_task(self._answer_request(message))
                    self._request_tasks.add(task)
                    task.add_done_callback(self._request_tasks.discard)
                else:
                    await self.events.put(message)
        except AgentError as exc:
            self.failure = exc
        except Exception as exc:  # noqa: BLE001 - any read failure ends the connection, reported once
            self.failure = AgentError(f"Codex app-server output failed: {type(exc).__name__}: {exc}")
            self.failure.__cause__ = exc
        finally:
            error = self.failure or AgentError("Codex app-server closed its output")
            self._closed_error = error
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            params: dict[str, Any] = {"error": str(self.failure)} if self.failure else {}
            await self.events.put({"method": "connection/closed", "params": params})

    def stderr_tail(self) -> str:
        """The last few KiB the app-server wrote to stderr, decoded leniently."""
        return self._stderr.decode("utf-8", errors="replace").strip()

    async def _drain_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        while chunk := await process.stderr.read(4096):
            self._stderr += chunk
            del self._stderr[: max(0, len(self._stderr) - STDERR_TAIL)]

    async def _frame(self, stdout: asyncio.StreamReader) -> bytes:
        """The next newline-terminated message, or b"" at end of output. A frame over the limit
        stops the connection with its size and the calls waiting on it, reported once."""
        try:
            return await stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                pending = ", ".join(sorted(self._methods.values())) or "none"
                raise AgentError(
                    f"Codex app-server output ended with an unterminated frame "
                    f"({len(exc.partial)} bytes; pending: {pending}); the connection was closed"
                ) from exc
            return b""
        except asyncio.LimitOverrunError as exc:
            pending = ", ".join(sorted(self._methods.values())) or "none"
            raise AgentError(
                f"Codex app-server sent a frame over {self.frame_limit} bytes (at least {exc.consumed} "
                f"read; pending: {pending}); the connection was closed"
            ) from exc

    async def _answer_request(self, message: dict[str, Any]) -> None:
        # Headless workers must not wait on an invisible approval prompt. The caller
        # can supply a narrower answer; failures still decline the request.
        result: dict[str, Any] = {"decision": "decline"}
        if self.handle_request is not None:
            with contextlib.suppress(Exception):
                result = await self.handle_request(message)
        with contextlib.suppress(Exception):
            await self._write({"id": message["id"], "result": result})
