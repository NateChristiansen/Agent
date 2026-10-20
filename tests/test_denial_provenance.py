"""The denial log records real refusals with who was refused.

Doctor's latency probe once made up nearly every logged denial, and worker denials (such
as a refused commit) left no event at all, since a worker's policy runs in-process."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli import telemetry
from agent_cli.hooks import decision_log

DENY = json.dumps(
    {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "chained commands"}}
)
PAYLOAD: dict[str, Any] = {
    "session_id": "s1",
    "tool_name": "Bash",
    "tool_input": {"command": "git status && git log -1"},
}


@pytest.fixture
def sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def sink_root(_root: Path) -> Path:
        return tmp_path

    monkeypatch.setattr(telemetry, "sink_root", sink_root)
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    monkeypatch.delenv(decision_log.PROBE_ENV, raising=False)
    return tmp_path


def _events(sink: Path) -> list[dict[str, Any]]:
    files = sorted((sink / ".agent" / "telemetry" / "events").glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def test_doctors_probe_samples_go_to_its_scratch_sink(
    sink: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scratch = tmp_path / "probe-scratch"
    monkeypatch.setenv(decision_log.PROBE_ENV, str(scratch))
    decision_log.denial(sink, "no-chaining", PAYLOAD, DENY, 0.01)
    assert _events(sink) == []
    assert len(_events(scratch)) == 1


def test_a_denial_inside_a_dispatch_names_the_worker_and_dispatch(
    sink: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-mac-11")
    decision_log.denial(sink, "no-chaining", PAYLOAD, DENY, 0.01)
    [event] = _events(sink)
    assert event["data"]["role"] == "worker" and event["dispatch"] == "001-mac-11"


def test_a_managers_denial_has_no_dispatch(sink: Path):
    decision_log.denial(sink, "no-chaining", PAYLOAD, DENY, 0.01)
    [event] = _events(sink)
    assert event["data"]["role"] == "session" and event["dispatch"] is None


def test_an_in_process_worker_denial_is_logged(sink: Path):
    decision_log.worker_denial(
        sink, "002-mac-01", "codex", "worker-policy", "uv run pyright > /tmp/x.json", "escapes the worktree"
    )
    [event] = _events(sink)
    assert event["provider"] == "codex" and event["dispatch"] == "002-mac-01"
    assert event["data"] | {"reason": None} == {
        "hook": "worker-policy",
        "role": "worker",
        "reason": None,
        "command": "uv",
        "class": "policy",
    }


def test_claudes_fall_through_permission_denial_is_logged(sink: Path, monkeypatch: pytest.MonkeyPatch):
    """Codex's phase-1 review: `can_use_tool` returned denials without telemetry."""
    import asyncio

    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    from agent_cli import repos as repos_mod
    from agent_cli.dispatch import supervisor

    def deny(*_args: object) -> tuple[str, str]:
        return "deny", "no rule covers this command"

    def keep(*_args: object) -> None:
        return None

    monkeypatch.setattr(supervisor, "decide", deny)
    monkeypatch.setattr(supervisor, "record_permission_event", keep)
    repo = repos_mod.Repo(name="Harness", short="harness", path=".", remote="x")
    callback = supervisor.permission_callback(sink, {"id": "001-mac-02"}, repo)
    context = ToolPermissionContext(signal=None, suggestions=[])
    result = asyncio.run(callback("Bash", {"command": "sed -i s/a/b/ x.py"}, context))
    assert isinstance(result, PermissionResultDeny)
    [event] = _events(sink)
    assert event["dispatch"] == "001-mac-02" and event["data"]["hook"] == "worker-permission"
