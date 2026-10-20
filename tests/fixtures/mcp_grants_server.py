"""A local stdio MCP server for the native grant acceptance cells (`native_mcp_grants.py`).

Standard library only, so `python -I mcp_grants_server.py --state <dir>` runs on any host
without the harness importable. Two tools:

- `read_nonce` returns the run nonce from `<state>/nonce`. It is the one tool a cell grants.
- `write_sentinel` is never granted. If a call ever reaches this process it first writes a
  marker under `<state>/sentinel/`, so an execution the client failed to block leaves
  evidence even when the response is lost.

Every request is appended to `<state>/calls.jsonl` (bounded) with this process's parent pid.
The start entry also records a few ancestors, read while they are alive: a launcher (a Windows
venv `python.exe`, the npm `codex` shim) can sit between the client and this process. Nothing
is written outside the state directory, and no environment value is read or recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SERVER_NAME = "n3grants"
ALLOWED_TOOL = "read_nonce"
SENTINEL_TOOL = "write_sentinel"
CALLS_FILE = "calls.jsonl"
NONCE_FILE = "nonce"
SENTINEL_DIR = "sentinel"
MAX_LOG_BYTES = 256 * 1024
MAX_FIELD = 200
DEFAULT_PROTOCOL = "2025-06-18"
MAX_ANCESTORS = 4

TOOLS = [
    {
        "name": ALLOWED_TOOL,
        "description": "Return this acceptance run's nonce. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": SENTINEL_TOOL,
        "description": (
            "Acceptance sentinel. Not granted to workers: the harness is expected to deny it. "
            "If it runs, it writes a marker file in the fixture's own state directory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "additionalProperties": False,
        },
    },
]


def _windows_parents() -> Callable[[int], int | None]:
    """The Toolhelp snapshot `agent_cli.winproc.process_entries` takes, inlined because this
    server runs under `python -I` without the harness importable."""
    assert sys.platform == "win32"
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    table: dict[int, int] = {}
    handle = k32.CreateToolhelp32Snapshot(0x2, 0)
    if handle != ctypes.c_void_p(-1).value:
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(entry)
            ok = k32.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = k32.Process32NextW(handle, ctypes.byref(entry))
        finally:
            k32.CloseHandle(handle)
    return table.get


def _posix_parent(pid: int) -> int | None:
    try:
        proc = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=False, timeout=2
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    value = proc.stdout.strip()
    return int(value) if value.isdigit() else None


def ancestors(limit: int = MAX_ANCESTORS) -> list[int]:
    """This process's parent, grandparent and so on, nearest first, at most `limit`."""
    try:
        parent_of = _windows_parents() if os.name == "nt" else _posix_parent
    except OSError, AttributeError, ValueError:
        return [os.getppid()]
    chain: list[int] = []
    current: int | None = os.getppid()
    while current and current > 0 and current not in chain and len(chain) < limit:
        chain.append(current)
        current = parent_of(current)
    return chain


class Server:
    def __init__(self, state: Path) -> None:
        self.state = state
        self.sentinel_calls = 0

    def log(self, event: dict[str, Any]) -> None:
        path = self.state / CALLS_FILE
        try:
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                return
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), **event}) + "\n")
        except OSError:
            pass

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        has_id = "id" in message
        tool = str(params.get("name") or "")[:MAX_FIELD] if method == "tools/call" else None
        self.log({"event": "request", "method": method[:MAX_FIELD], "tool": tool, "has_id": has_id})
        if not has_id:
            return None
        if method == "initialize":
            requested = params.get("protocolVersion")
            result: dict[str, Any] = {
                "protocolVersion": requested if isinstance(requested, str) else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = self.call(str(params.get("name") or ""))
        else:
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "unknown method"},
            }
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def call(self, name: str) -> dict[str, Any]:
        if name == ALLOWED_TOOL:
            nonce = (self.state / NONCE_FILE).read_text(encoding="utf-8").strip()
            self.log({"event": "result", "tool": ALLOWED_TOOL, "nonce_returned": True})
            return {"content": [{"type": "text", "text": f"n3 fixture nonce: {nonce}"}], "isError": False}
        if name == SENTINEL_TOOL:
            self.sentinel_calls += 1
            marker = self.state / SENTINEL_DIR / f"executed-{os.getpid()}-{self.sentinel_calls}.json"
            marker.parent.mkdir(exist_ok=True)
            marker.write_text(
                json.dumps({"tool": SENTINEL_TOOL, "pid": os.getpid()}) + "\n", encoding="utf-8"
            )
            self.log({"event": "result", "tool": SENTINEL_TOOL, "executed": True})
            return {"content": [{"type": "text", "text": "sentinel executed"}], "isError": False}
        return {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}


def serve(state: Path) -> int:
    if not state.is_dir():
        sys.stderr.write("state directory is missing\n")
        return 2
    server = Server(state)
    server.log({"event": "start", "ancestors": ancestors()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            server.log({"event": "unparsed"})
            continue
        if not isinstance(message, dict):
            continue
        reply = server.handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    server.log({"event": "stop"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP grant acceptance fixture server")
    parser.add_argument("--state", required=True, type=Path)
    return serve(parser.parse_args(argv).state.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
