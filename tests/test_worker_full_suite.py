"""A dispatch worker may run named tests, never the whole suite (owner 2026-10-04)."""

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from agent_cli.dispatch.codex_permissions import decision
from agent_cli.dispatch.rules import sdk_hooks
from agent_cli.hooks.worker_tests import REASON, full_suite
from dispatch_helpers import droot  # noqa: F401 -- pytest fixture


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest",
        "uv run pytest -q",
        "uv run pytest -x -n0",
        "uv run pytest tests",
        "uv run pytest tests/",
        "uv run pytest -k notices",
        "uv run pytest -m slow -q",
        "pytest",
        ".venv/bin/pytest -q",
        "uv run python -m pytest -q",
        "python3 -m pytest",
        "uv run ruff check src tests ; uv run pytest -q",
        "AGENT_X=1 uv run pytest",
        "uv run pytest -q\n",
    ],
)
def test_whole_suite_runs_are_refused(command: str):
    assert full_suite(command) == REASON


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests/test_peers.py",
        "uv run pytest tests/test_peers.py tests/test_peer_wake.py -q",
        "uv run pytest -k fence tests/test_dispatch_notices.py",
        "uv run pytest tests/test_peers.py::test_take -x",
        "uv run pytest tests/hooks/",
        "uv run python -m pytest tests/test_peers.py",
        "uv run ruff check src tests",
        "uv run pyright",
        "git diff --stat",
        "grep -rn pytest src",
    ],
)
def test_named_tests_and_other_commands_pass(command: str):
    assert full_suite(command) is None


def test_claude_worker_pretooluse_refuses_the_whole_suite(tmp_path: Path):
    hooks = sdk_hooks(tmp_path, tmp_path)
    callback = hooks["PreToolUse"][0].hooks[0]
    payload: Any = {"tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
    result = cast(dict[str, Any], asyncio.run(callback(payload, None, {"signal": None})))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == REASON
    payload["tool_input"]["command"] = "uv run pytest tests/test_peers.py"
    assert asyncio.run(callback(payload, None, {"signal": None})) == {}


def test_codex_worker_refuses_the_whole_suite(droot: Path, tmp_path: Path, monkeypatch):
    from agent_cli.dispatch import codex_permissions

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(
        codex_permissions.state, "load", lambda *_args: {"repo": "alpha", "worktree": str(worktree)}
    )
    denied = decision(droot, "eval", {"tool_name": "Bash", "tool_input": {"command": "uv run pytest"}})
    assert denied == ("deny", REASON)
    named = decision(
        droot, "eval", {"tool_name": "Bash", "tool_input": {"command": "uv run pytest tests/test_peers.py"}}
    )
    assert named[1] != REASON
