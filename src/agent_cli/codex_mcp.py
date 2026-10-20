"""Project-scoped Codex registration for the harness's manager MCP server."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from .core import atomic_write

CONFIG = Path(".codex/config.toml")
BEGIN = "# BEGIN agent setup: Codex agent MCP"
END = "# END agent setup: Codex agent MCP"


def command(root: Path, *, windows: bool | None = None) -> Path:
    """The installed entry point in this clone's selected uv environment."""
    if windows is None:
        windows = os.name == "nt"
    return (root / ".venv" / ("Scripts/python.exe" if windows else "bin/agent")).resolve()


def expected(root: Path, *, windows: bool | None = None) -> dict[str, Any]:
    resolved = root.resolve()
    if windows is None:
        windows = os.name == "nt"
    module = ["-I", "-m", "agent_cli"] if windows else []  # not agent.exe
    return {
        "command": str(command(resolved, windows=windows)),
        "args": [*module, "--root", str(resolved), "mcp", "--profile", "manager"],
        "cwd": str(resolved),
        "env_vars": ["SSH_AUTH_SOCK"],
        "required": True,
        "startup_timeout_sec": 30,
    }


def configure(root: Path, *, windows: bool | None = None) -> tuple[str, str]:
    """Replace only our generated block; refuse an unmanaged `agent` registration."""
    path = root / CONFIG
    if path.is_symlink():
        return "refused", f"{CONFIG} is a symlink; update it yourself"
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    try:
        tomllib.loads(current)
    except tomllib.TOMLDecodeError as exc:
        return "refused", f"{CONFIG} is invalid TOML: {exc}"
    if BEGIN in current or END in current:
        if current.count(BEGIN) != 1 or current.count(END) != 1:
            return "refused", f"{CONFIG} has incomplete or duplicate Codex agent MCP markers"
        before, rest = current.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        other = before + after.removeprefix("\n")
    else:
        other = current
    try:
        unmanaged = tomllib.loads(other)
    except tomllib.TOMLDecodeError as exc:
        return "refused", f"{CONFIG} conflicts with generated MCP section: {exc}"
    if "agent" in unmanaged.get("mcp_servers", {}):
        return "refused", f"{CONFIG} already has an unmanaged mcp_servers.agent entry"

    entry = expected(root, windows=windows)
    block = (
        f"{BEGIN}\n"
        "[mcp_servers.agent]\n"
        f"command = {json.dumps(entry['command'])}\n"
        f"args = {json.dumps(entry['args'])}\n"
        f"cwd = {json.dumps(entry['cwd'])}\n"
        f"env_vars = {json.dumps(entry['env_vars'])}\n"
        "required = true\n"
        "startup_timeout_sec = 30\n"
        f"{END}\n"
    )
    updated = other.rstrip("\n") + ("\n\n" if other.strip() else "") + block
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        return "refused", f"{CONFIG} conflicts with generated MCP section: {exc}"
    if updated == current:
        return "already", f"{CONFIG} registers the agent MCP server"
    atomic_write(path, updated)
    return "done", f"{CONFIG} registers the agent MCP server"


def probe(root: Path, entry: dict[str, Any]) -> tuple[bool, str]:
    """Start the configured server and make one read-only MCP tool call."""
    import anyio
    from mcp import Client, StdioServerParameters

    async def check() -> None:
        params = StdioServerParameters(
            command=str(entry["command"]),
            args=[str(arg) for arg in entry["args"]],
            cwd=str(entry["cwd"]),
        )
        async with Client(params, read_timeout_seconds=10) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            if "feature_list" not in names or "feature_show" not in names:
                raise ValueError("manager read tools are absent")
            result = await client.call_tool("feature_list", {})
            if result.is_error:
                raise ValueError(f"feature_list returned an error: {result.content}")

    try:
        anyio.run(check)
    except Exception as exc:  # noqa: BLE001 — doctor reports a broken server, never crashes
        return False, f"agent MCP read-only probe failed: {type(exc).__name__}: {exc}"
    return True, f"agent MCP tools start and feature_list reads {root}"
