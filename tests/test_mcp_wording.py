"""A CLI reference in a message a model reads over MCP names the tool that does it, and the
routed tools the map points at work over MCP."""

from __future__ import annotations

import pytest

from agent_cli.mcp import coverage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("unknown repo 'x'; see `agent repos list`", "unknown repo 'x'; see the `repos_list` tool"),
        ("run `bin/agent dispatch show 12`", "run the `dispatch_read` tool with action `show` (12)"),
        (
            "resume with `bin/agent dispatch resume 001-01 --budget 9 --reason 'big one'`",
            "resume with the `dispatch` tool with action `resume` (001-01, budget_usd=9, budget_reason=big one)",
        ),
        ("run `agent lease clear x --yes`", "run the `lease` tool with action `clear` (x, confirm=True)"),
        ("run `agent status render`", "run the `status_render` tool"),
        (
            "run `agent --json journal collect 2026-09-30`",
            "run the `harness` tool with action `journal_collect` (2026-09-30)",
        ),
        ("run `agent log 052 note x`", "run the `log` tool (052, note, x)"),
        ("run `agent setup --notes`", "run `agent setup --notes` (the owner runs this)"),
        ("no CLI here", "no CLI here"),
    ],
)
def test_mcp_wording_names_the_tool(text, want):
    assert coverage.mcp_wording(text) == want


def test_tool_for_and_person_only_name_the_route():
    assert coverage.tool_for("verify", "show") == ("harness", "verify_show")
    assert coverage.tool_for("doctor", None) == ("harness", "doctor")
    assert coverage.tool_for("peer", "send") == ("peer", "send")
    assert coverage.tool_for("log", "052") == ("log", None)
    assert coverage.tool_for("export", None) is None
    assert coverage.person_only("export", "whatever")
    assert coverage.person_only("procs", "reap")
    assert not coverage.person_only("procs", "list")


@pytest.mark.anyio
async def test_peer_send_is_callable_as_an_mcp_tool(tmp_root, monkeypatch):
    import os

    from mcp import Client

    from agent_cli import peers, sessions
    from agent_cli.mcp import server as mcp_server

    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    monkeypatch.setattr("agent_cli.ops.peers._ring_socket", lambda path: None)
    sessions.set_record(tmp_root, "me", name="mgr-me", pid=os.getpid())
    sessions.set_record(tmp_root, "codex", name="mgr-codex", pid=os.getpid())
    server = mcp_server.build_server(tmp_root, "manager")
    async with Client(server) as client:
        r = await client.call_tool(
            "peer", {"action": "send", "to": "mgr-codex", "message": "review 3", "session_id": "me"}
        )
        assert r.is_error is False
        r = await client.call_tool("peer", {"action": "send", "to": "mgr-codex", "session_id": "me"})
        assert r.is_error is True  # message required
    assert [row["text"] for row in peers.peek(tmp_root, "codex")] == ["review 3"]
