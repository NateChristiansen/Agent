"""The served MCP server runs every tool call in a fresh process on the code
on disk, and warns only when the tool schemas changed since it started."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from agent_cli import hooks
from agent_cli.core import AgentError
from agent_cli.mcp import fresh, staleness
from agent_cli.mcp import server as mcp_server
from agent_cli.mcp.tools import Ctx, Op, ToolSpec

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("notes_remote")]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def seeded(tmp_root: Path, run) -> Path:
    run(["feature", "new", "x", "--title", "X"])
    run(["task", "add", "1", "write the parser"])
    run(["feature", "use", "1", "--session-id", "s1"])
    return tmp_root


async def test_a_fresh_server_runs_the_op_in_a_child_process(seeded: Path):
    server = mcp_server.build_server(seeded, "manager", ["task_done", "feature_list"], fresh=True)
    async with Client(server) as client:
        r = await client.call_tool("task_done", {"task": "1", "feature": "1", "note": "fresh"})
        assert r.structured_content["changed"] is True
        listed = await client.call_tool("feature_list", {})
        assert listed.structured_content["count"] == 1
        # an AgentError in the child is a tool error the model can read, not a crash
        bad = await client.call_tool("task_done", {"task": "9", "feature": "1"})
        assert bad.is_error
    assert "- [x] write the parser (fresh)" in (seeded / "features/001-x/plan.md").read_text()


async def test_a_tool_parameter_called_name_reaches_the_child(seeded: Path):
    """052: `_call_fresh(name, ...)` took a tool's own `name` argument as a duplicate keyword, so
    every served `repos_sync(name=...)` failed with a bare "Error executing tool repos_sync"."""
    server = mcp_server.build_server(seeded, "manager", ["repos_status"], fresh=True)
    async with Client(server) as client:
        r = await client.call_tool("repos_status", {"name": "no-such-repo"})
    text = "\n".join(getattr(c, "text", "") for c in r.content)
    assert "unknown repo 'no-such-repo'" in text


async def test_the_child_uses_the_code_on_disk_not_the_server_s_tables(seeded: Path, monkeypatch):
    """A tool that exists only in the server process's imported tables is unknown to the child,
    which imports the tables fresh. That is the property: the child never sees the server's
    in-memory copy of the code."""

    def ping(ctx: Ctx) -> Op:
        return Op({"word": "pong"}, "said pong")

    monkeypatch.setitem(mcp_server.MANAGER_TOOLS, "ping", ToolSpec(ping, "Ping", "test tool", read_only=True))
    server = mcp_server.build_server(seeded, "manager", ["ping"], fresh=True)
    async with Client(server) as client:
        r = await client.call_tool("ping", {})
    assert r.is_error
    assert "reconnect /mcp" in "\n".join(getattr(c, "text", "") for c in r.content)


# ---------------------------------------------------------------- staleness for a fresh server


def _stamp(root: Path, **fields: object) -> None:
    path = root / staleness.STAMP_DIR / f"server-{os.getpid()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "profile": "manager", **fields}), encoding="utf-8")


def _schema(root: Path, text: str) -> None:
    path = root / "src" / "agent_cli" / "mcp" / "specs_features.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_fresh_server_is_current_after_a_code_edit(tmp_root: Path):
    _schema(tmp_root, "TOOLS = {}\n")
    _stamp(tmp_root, fresh=True, code_mtime=0.0, schema_hash=staleness.schema_hash(tmp_root))
    (tmp_root / "src/agent_cli/ops.py").write_text("x = 2\n", encoding="utf-8")  # newer code
    assert staleness.stale_servers(tmp_root) == []
    assert hooks.mcp_stale(tmp_root, {"tool_name": "mcp__agent__log"}) == ""


def test_a_fresh_server_warns_when_a_tool_schema_changed(tmp_root: Path):
    _schema(tmp_root, "TOOLS = {}\n")
    _stamp(tmp_root, fresh=True, code_mtime=0.0, schema_hash=staleness.schema_hash(tmp_root))
    _schema(tmp_root, "TOOLS = {'ping': 1}\n")
    line = staleness.stale_line(tmp_root)
    assert "runs current code" in line and "/mcp" in line
    out = json.loads(hooks.mcp_stale(tmp_root, {"tool_name": "mcp__agent__log"}))
    assert "permissionDecision" not in out["hookSpecificOutput"]


def _child_replies(monkeypatch: pytest.MonkeyPatch, reply: object) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(reply) + "\n", stderr="")

    monkeypatch.setattr(fresh.subprocess, "run", fake_run)


@pytest.mark.parametrize("ok", ["text", ["a"], 7, True, "", 0, []])
def test_an_ok_that_is_not_an_object_is_a_bad_reply(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, ok: object
):
    _child_replies(monkeypatch, {"ok": ok})
    with pytest.raises(AgentError):
        fresh.run_fresh(tmp_root, "manager", "feature_list", {})


@pytest.mark.parametrize("reply", [{}, {"ok": None}])
def test_a_missing_or_null_ok_is_an_empty_result(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, reply: object
):
    _child_replies(monkeypatch, reply)
    assert fresh.run_fresh(tmp_root, "manager", "feature_list", {}) == {}


def test_serve_stamps_a_fresh_server(tmp_root: Path):
    _schema(tmp_root, "TOOLS = {}\n")
    path = staleness.write_stamp(tmp_root, "manager", fresh=True)
    assert path is not None
    record = json.loads(path.read_text())
    assert record["fresh"] is True and record["schema_hash"] == staleness.schema_hash(tmp_root)
