"""A Claude subagent inherits the manager's session id and cannot remap its record."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_subagent_cannot_remap_manager_session(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "one", "--title", "One"])
    run(["feature", "new", "two", "--title", "Two"])
    assert run(["feature", "use", "1", "--session-id", "manager"]).code == 0
    manager = tmp_root / ".agent/sessions/manager.json"
    before = manager.read_bytes()

    monkeypatch.setenv("CLAUDE_AGENT_ID", "agent-child")
    use = run(["feature", "use", "2", "--session-id", "manager"])
    session = run(["session", "set", "--session-id", "manager", "--feature", "2"])
    assert use.code != 0 and "subagent cannot remap" in use.err
    assert session.code != 0 and "subagent cannot rewrite" in session.err
    assert manager.read_bytes() == before
