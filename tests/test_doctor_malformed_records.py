"""Doctor's MCP checks on malformed settings and .mcp.json: each reports or skips the bad shape
instead of raising out of `agent doctor`."""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.doctor import checks_mcp, checks_notes


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _settings(root: Path, data: object) -> None:
    _write(root / ".claude" / "settings.json", data)


def test_mcp_servers_that_is_not_an_object_reports_no_agent_entry(tmp_root: Path):
    _write(tmp_root / ".mcp.json", {"mcpServers": ["agent"]})
    rows = [r for r in checks_mcp.check_mcp_wiring(tmp_root) if r["check"] == "mcp wiring"]
    assert rows[-1]["level"] == "fail"
    assert "no mcpServers.agent entry" in rows[-1]["detail"]


def test_local_rules_given_as_a_string_are_no_rules(tmp_root: Path):
    _write(tmp_root / ".claude" / "settings.local.json", {"permissions": {"allow": "mcp__x__*"}})
    assert checks_mcp._local_rules(tmp_root) == []


def test_a_permission_rule_that_is_not_a_string_is_skipped_when_matching_servers():
    sdata: dict[str, object] = {"permissions": {"allow": [5, "mcp__x__*"]}}
    rows = checks_mcp._mcp_servers({"agent": {}, "x": {}}, sdata, ["agent", "x"])
    assert [r["level"] for r in rows] == ["ok"]


def test_enabled_servers_given_as_a_string_enable_nothing(tmp_root: Path):
    _settings(tmp_root, {"enabledMcpjsonServers": "ab"})
    (tmp_root / ".claude" / "settings.local.json").unlink(missing_ok=True)
    assert checks_notes.enabled_servers(tmp_root) == []
