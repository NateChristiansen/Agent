"""The `harness` MCP tool: the CLI-only commands a model runs, served over MCP."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_cli.core import EXIT_NOT_FOUND
from agent_cli.mcp import specs_harness
from agent_cli.mcp.tools import Ctx
from agent_cli.ops import Op


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_scratch_path_and_notes_root_run_over_mcp(tmp_root):
    from mcp import Client

    from agent_cli.mcp import server as mcp_server

    server = mcp_server.build_server(tmp_root, "manager")
    async with Client(server) as client:
        r = await client.call_tool("harness", {"action": "scratch_path"})
        assert r.is_error is False
        assert r.structured_content["ok"] is True
        assert (tmp_root / ".agent" / "scratch").is_dir()
        r = await client.call_tool("harness", {"action": "notes_root"})
        assert r.is_error is False
        assert r.structured_content["root"]


@pytest.mark.anyio
async def test_a_missing_parameter_is_a_tool_error_that_names_it(tmp_root):
    from mcp import Client

    from agent_cli.mcp import server as mcp_server

    server = mcp_server.build_server(tmp_root, "manager")
    async with Client(server) as client:
        r = await client.call_tool("harness", {"action": "journal_write", "date": "2026-09-30"})
        assert r.is_error is True
        assert "`file`" in getattr(r.content[0], "text", "")
        r = await client.call_tool("harness", {"action": "not_an_action"})
        assert r.is_error is True


def test_a_failing_check_is_data_not_an_error(tmp_root, monkeypatch):
    monkeypatch.setattr(
        "agent_cli.ops.verify_show",
        lambda root, rev: Op({"applies": False, "why": "no record"}, "no record", code=EXIT_NOT_FOUND),
    )
    op = specs_harness.harness(Ctx(root=tmp_root, profile="manager"), action="verify_show", ref="HEAD")
    assert op.data["ok"] is False
    assert op.data["why"] == "no record"


def test_the_retired_check_actions_are_gone():
    import typing

    actions = set(typing.get_args(specs_harness.HarnessAction))
    assert not {a for a in actions if a.endswith(("_check", "_pre_push"))}


def test_long_output_spills_text_and_data_to_a_unique_scratch_file(tmp_root, monkeypatch):
    import json

    long_text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    rows = [{"check": f"c{i}", "detail": "y" * 80} for i in range(100)]
    monkeypatch.setattr("agent_cli.ops.doctor", lambda root: Op(rows, long_text))
    ctx = Ctx(root=tmp_root, profile="manager")
    op = specs_harness.harness(ctx, action="doctor")
    assert len(op.text) < specs_harness.SUMMARY_LIMIT
    assert op.data["truncated"] is True and op.data["ok"] is True
    assert len(json.dumps(op.data)) < specs_harness.SUMMARY_LIMIT
    saved = json.loads(open(op.data["full_output"], encoding="utf-8").read())
    assert saved["text"] == long_text and saved["data"]["items"] == rows
    # a second call in the same second writes its own file
    assert specs_harness.harness(ctx, action="doctor").data["full_output"] != op.data["full_output"]


def test_a_long_doctor_keeps_its_summary_line_through_truncation(tmp_root, monkeypatch):
    from agent_cli.doctor import check as check_mod
    from agent_cli.ops import infra

    checks = [check_mod.new_check(f"c{i}", "warn" if i == 0 else "ok", "d" * 200) for i in range(25)]
    monkeypatch.setattr(infra.doctor_mod, "run", lambda root: checks)
    op = specs_harness.harness(Ctx(root=tmp_root, profile="manager"), action="doctor")
    assert op.data["truncated"] is True
    assert op.text.splitlines()[0] == "24 OK, 1 WARN, 0 FAIL"


def test_one_very_long_line_still_gives_a_short_summary(tmp_root, monkeypatch):
    monkeypatch.setattr("agent_cli.ops.doctor", lambda root: Op([], "z" * 50_000))
    op = specs_harness.harness(Ctx(root=tmp_root, profile="manager"), action="doctor")
    assert len(op.text) < specs_harness.SUMMARY_LIMIT


def test_short_output_is_returned_whole(tmp_root, monkeypatch):
    monkeypatch.setattr("agent_cli.ops.doctor", lambda root: Op([{"check": "a"}], "1 check"))
    op = specs_harness.harness(Ctx(root=tmp_root, profile="manager"), action="doctor")
    assert op.data == {"items": [{"check": "a"}], "ok": True} and op.text == "1 check"


@pytest.mark.parametrize("apply,exit_code", [(False, 0), (True, 0), (True, 1)])
def test_tidy_keeps_fast_results_and_reports_failure_logs(tmp_root, monkeypatch, apply, exit_code):
    def spawn(argv, **kwargs):
        assert argv[-1] == "--apply" if apply else argv[-1] == "tidy"
        assert kwargs["start_new_session"] is True
        kwargs["stdout"].write(json.dumps({"dry_run": not apply, "trees": [], "branches": []}).encode())
        kwargs["stdout"].flush()

        class Child:
            pid = 123

            def wait(self, *, timeout):
                assert timeout == 60
                return exit_code

        return Child()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = specs_harness.harness(Ctx(root=tmp_root, profile="manager"), action="tidy", apply=apply)
    assert result.data["detached"] is False
    assert result.data["ok"] is (exit_code == 0)
    assert Path(result.data["log"]).exists()


def test_tidy_timeout_leaves_child_running_and_returns_paths(tmp_root, monkeypatch):
    def spawn(argv, **kwargs):
        class Child:
            pid = 123

            def wait(self, *, timeout):
                assert timeout == 0.5
                raise subprocess.TimeoutExpired(argv, timeout)

        return Child()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = specs_harness.harness(Ctx(root=tmp_root, profile="manager"), action="tidy", wait=0.5)
    assert result.data["detached"] is True
    assert result.data["ok"] is True
    assert result.data["pid"] == 123
    assert Path(result.data["log"]).exists() and Path(result.data["result"]).exists()
