"""Round trip through the real entry point: `bin/agent mcp --profile manager` as a stdio
subprocess, the way Claude Code launches it from .mcp.json."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("notes_remote")]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _params(tmp_root: Path) -> StdioServerParameters:
    # bin/agent exports AGENT_ROOT from its own location. The fixture root has to go in as
    # --root. The env var alone would point the server at the real vault.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["AGENT_NOW"] = os.environ["AGENT_NOW"]
    return StdioServerParameters(
        command=str(ROOT / "bin/agent"),
        args=["--root", str(tmp_root), "mcp", "--profile", "manager"],
        env=env,
        cwd=str(tmp_root),
    )


async def test_stdio_round_trip(tmp_root: Path):
    async with Client(_params(tmp_root), read_timeout_seconds=30) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "log" in names and "feature_new" in names and "task_done" in names
        r = await client.call_tool("feature_new", {"slug": "over-stdio", "title": "Over stdio", "use": False})
        assert r.is_error is False, r.content
        assert r.structured_content["id"] == 1
        r = await client.call_tool(
            "log",
            {"kind": "note", "text": 'the server is up: quotes " and `ticks`\nsecond line', "feature": "1"},
        )
        assert r.is_error is False, r.content
        assert r.structured_content["ts"] == "2026-09-03 14:05"
        # the server is still healthy after writes (nothing leaked onto stdout)
        names_again = {t.name for t in (await client.list_tools(cache_mode="bypass")).tools}
        assert names_again == names
    log_md = (tmp_root / "features/001-over-stdio/log.md").read_text()
    assert '> the server is up: quotes " and `ticks`' in log_md
    assert "> second line" in log_md
    assert (tmp_root / "Dashboard.md").exists()


async def test_stdio_worker_refuses_without_dispatch_env(tmp_root: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "AGENT_"))}
    params = StdioServerParameters(
        command=str(ROOT / "bin/agent"),
        args=["--root", str(tmp_root), "mcp", "--profile", "worker"],
        env=env,
        cwd=str(tmp_root),
    )
    with pytest.raises(Exception):  # noqa: B017 — the process exits 2 before the handshake completes
        async with Client(params, read_timeout_seconds=10) as client:
            await client.list_tools()
