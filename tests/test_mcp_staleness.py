"""enforced: a live MCP server that predates the code it would run says so.

Filed advisory on 2026-09-15, after a stale server spent two hours re-rendering the old
Dashboard.md over the CLI's fresh one. The entry named this check as the enforced candidate
"not built until a second sighting". The second sighting came the next night, inside the same
feature: `raw_preserve` through the server returned three transcripts because it was running
the `capture.py` it had imported before the subagent-transcript fix, while `bin/agent raw
preserve 17` returned four.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_cli import hooks
from agent_cli.mcp import staleness


def _stamp(root: Path, pid: int, code_mtime: float) -> Path:
    path = root / staleness.STAMP_DIR / f"server-{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "profile": "manager", "started_at": "x", "code_mtime": code_mtime}),
        encoding="utf-8",
    )
    return path


def _src(root: Path, mtime: float) -> Path:
    src = root / "src" / "agent_cli"
    src.mkdir(parents=True, exist_ok=True)
    path = src / "thing.py"
    path.write_text("x = 1\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_a_server_older_than_the_code_is_stale(tmp_root: Path):
    _src(tmp_root, 2_000.0)
    _stamp(tmp_root, os.getpid(), 1_000.0)  # imported before the edit
    stale = staleness.stale_servers(tmp_root)
    assert [s["pid"] for s in stale] == [os.getpid()]
    line = staleness.stale_line(tmp_root)
    assert "OLD code" in line and "/mcp" in line


def test_a_server_newer_than_the_code_is_current(tmp_root: Path):
    _src(tmp_root, 1_000.0)
    _stamp(tmp_root, os.getpid(), 2_000.0)
    assert staleness.stale_servers(tmp_root) == []
    assert staleness.stale_line(tmp_root) == ""


def test_a_dead_server_s_stamp_is_swept(tmp_root: Path):
    """A stamp whose process is gone describes a server that answers nothing."""
    _src(tmp_root, 2_000.0)
    path = _stamp(tmp_root, 999_999, 1_000.0)
    assert staleness.stale_servers(tmp_root) == []
    assert not path.exists()


def test_a_stamp_whose_code_mtime_is_a_numeric_string_reads_as_stale(tmp_root: Path):
    _src(tmp_root, 2_000.0)
    path = _stamp(tmp_root, os.getpid(), 3_000.0)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["code_mtime"] = "3000.0"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert [s["pid"] for s in staleness.stale_servers(tmp_root)] == [os.getpid()]


def test_a_stamp_whose_pid_is_not_an_int_is_swept(tmp_root: Path):
    _src(tmp_root, 2_000.0)
    path = _stamp(tmp_root, 4242, 1_000.0)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["pid"] = str(os.getpid())
    path.write_text(json.dumps(record), encoding="utf-8")
    assert staleness.stale_servers(tmp_root) == []
    assert not path.exists()


def test_no_stamps_at_all_is_not_stale(tmp_root: Path):
    _src(tmp_root, 2_000.0)
    assert staleness.stale_line(tmp_root) == ""


def test_write_stamp_records_this_process_and_the_code_it_imported(tmp_root: Path):
    _src(tmp_root, 1_500.0)
    path = staleness.write_stamp(tmp_root, "manager")
    assert path is not None
    record = json.loads(path.read_text())
    assert record["pid"] == os.getpid()
    assert record["profile"] == "manager"
    assert record["code_mtime"] == 1_500.0
    # a server that just started is never stale against the tree it just read
    assert staleness.stale_line(tmp_root) == ""


# ---------------------------------------------------------------- the hook


def _pre(tool: str) -> dict[str, object]:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}


def test_the_hook_warns_on_an_mcp_call_when_the_server_is_stale(tmp_root: Path):
    _src(tmp_root, 2_000.0)
    _stamp(tmp_root, os.getpid(), 1_000.0)
    out = hooks.mcp_stale(tmp_root, _pre("mcp__agent__task_done"))
    payload = json.loads(out)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PreToolUse"
    assert "OLD code" in payload["additionalContext"]
    # a warning, never a denial: the stale server still answers correctly for what did not
    # change, and denying every MCP call while editing the harness would wedge it
    assert "permissionDecision" not in payload


def test_the_hook_ignores_other_tools(tmp_root: Path):
    _src(tmp_root, 2_000.0)
    _stamp(tmp_root, os.getpid(), 1_000.0)
    assert hooks.mcp_stale(tmp_root, _pre("Bash")) == ""
    assert hooks.mcp_stale(tmp_root, _pre("mcp__tracker__get_ticket")) == ""


def test_the_hook_is_silent_when_the_server_is_current(tmp_root: Path):
    _src(tmp_root, 1_000.0)
    _stamp(tmp_root, os.getpid(), 2_000.0)
    assert hooks.mcp_stale(tmp_root, _pre("mcp__agent__log")) == ""


def test_the_hook_is_wired_for_mcp_tools(tmp_root: Path):
    """The matcher has to name the MCP tools, or the check never runs where it matters."""
    from agent_cli.hooks.groups import wired

    settings = json.loads((Path(__file__).resolve().parents[1] / ".claude" / "settings.json").read_text())
    for tool in ("mcp__agent__log", "mcp__agent__dispatch", "mcp__agent__peer"):
        assert wired(settings, "PreToolUse", tool)[0] == "mcp-stale", tool


def test_a_session_is_warned_once_per_staleness(tmp_root: Path):
    """One manager session saw the same warning line repeatedly."""
    _src(tmp_root, 2_000.0)
    _stamp(tmp_root, os.getpid(), 1_000.0)
    first = {**_pre("mcp__agent__dispatch"), "session_id": "s-1"}
    assert hooks.mcp_stale(tmp_root, first)
    assert hooks.mcp_stale(tmp_root, first) == ""
    assert hooks.mcp_stale(tmp_root, {**first, "session_id": "s-2"})  # another session is told
    from agent_cli.mcp import staleness

    real = staleness.stale_line
    staleness.stale_line = lambda root: real(root) + " (another change)"  # a different staleness
    try:
        assert hooks.mcp_stale(tmp_root, first)
    finally:
        staleness.stale_line = real


def test_session_markers_never_collide_and_another_edit_warns_again(tmp_root: Path):
    """Codex's phase-F review: `session/a` and `session?a` shared a marker, and another source
    edit on the same stale server stayed silent."""
    _src(tmp_root, 2_000.0)
    _stamp(tmp_root, os.getpid(), 1_000.0)
    a, b = ({**_pre("mcp__agent__dispatch"), "session_id": s} for s in ("session/a", "session?a"))
    assert hooks.mcp_stale(tmp_root, a)
    assert hooks.mcp_stale(tmp_root, b)  # its own marker
    assert hooks.mcp_stale(tmp_root, a) == ""
    _src(tmp_root, 2_500.0)  # another edit, same stale server
    assert hooks.mcp_stale(tmp_root, a)


def test_old_markers_are_swept(tmp_root: Path):
    _src(tmp_root, 2_000.0)
    _stamp(tmp_root, os.getpid(), 1_000.0)
    folder = tmp_root / ".agent" / "mcp"
    folder.mkdir(parents=True, exist_ok=True)
    old = folder / "warned-old.txt"
    old.write_text("x")
    os.utime(old, (1.0, 1.0))
    assert hooks.mcp_stale(tmp_root, {**_pre("mcp__agent__dispatch"), "session_id": "s"})
    assert not old.exists()
