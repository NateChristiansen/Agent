"""The land warning (P5 Option C): a landing that changes hook wiring, hook names, MCP
tools or state formats names the live managers that must restart, and the MCP calls that moved.
It never blocks a landing. The landing's `land.op` telemetry is checked here too."""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli import land, land_warning, ops
from land_helpers import _land_template, git, harness  # noqa: F401 -- fixtures

RUN = 'NAMES = (\n    "session-start",\n    "probe-cwd-guard",\n)\n'
GROUPS = 'GROUPS = (\n    Group("claude-pre-bash", "claude", "PreToolUse", "Bash", ()),\n)\n'
TOOLS = 'DispatchAction = Literal["new", "show", "list"]\n'
SPECS = "def dispatch(ctx, action, dispatch_id=None, case=None):\n    pass\n"
LIVE = {
    "managers": [
        {"session_id": "b85e0000-0000-4000", "name": "mgr-001-dispatch", "state": "idle"},
        {"session_id": "01a10000-0000-7000", "name": "mgr-002-codex", "state": "working"},
    ],
    "workers": [{"id": "001-mac-09", "state": "running"}, {"id": "002-mac-01", "state": "done"}],
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def _branch(root: Path, name: str, files: dict[str, str]) -> None:
    git(root, "switch", "-q", "-c", name, "main")
    _write(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"{name} change")
    git(root, "switch", "-q", "main")


def _base(root: Path) -> None:
    _write(
        root,
        {
            "src/agent_cli/hooks/run.py": RUN,
            "src/agent_cli/hooks/groups.py": GROUPS,
            "src/agent_cli/mcp/tools.py": TOOLS,
            "src/agent_cli/mcp/specs_dispatch.py": SPECS,
            ".claude/settings.json": "{}\n",
        },
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "a harness with hooks and MCP")
    (root / ".agent").mkdir(exist_ok=True)
    (root / ".agent" / "live.json").write_text(json.dumps(LIVE))


def test_a_renamed_hook_and_a_moved_action_name_who_must_restart(harness: Path):  # noqa: F811
    _base(harness)
    _branch(
        harness,
        "rename",
        {
            "src/agent_cli/hooks/run.py": RUN.replace("probe-cwd-guard", "cwd-guard"),
            "src/agent_cli/mcp/tools.py": TOOLS.replace('"show", ', ""),
            "src/agent_cli/mcp/specs_dispatch.py": SPECS.replace(", case=None", ""),
            ".claude/settings.json": '{"hooks": {}}\n',
        },
    )
    result = ops.harness.land(harness, branch="rename", strategy="ff")
    found = result.data["restart"]
    assert found["wiring"] == [".claude/settings.json"]
    assert found["hooks_removed"] == ["probe-cwd-guard"]
    assert found["mcp_moved"] == [
        "DispatchAction: value show removed or moved",
        "tool dispatch: parameter case removed or moved",
    ]
    assert found["managers"] == ["mgr-001-dispatch (b85e0000)", "mgr-002-codex (01a10000)"]
    assert found["workers"] == ["001-mac-09"]
    assert "RESTART WARNING" in result.text and "Restart these managers: mgr-001-dispatch" in result.text
    assert "001-mac-09" in result.text


def test_an_additive_mcp_change_names_no_moved_call(harness: Path):  # noqa: F811
    _base(harness)
    _branch(
        harness,
        "additive",
        {
            "src/agent_cli/mcp/tools.py": TOOLS.replace('"list"', '"list", "consume"'),
            "src/agent_cli/mcp/specs_dispatch.py": SPECS.replace("case=None", "case=None, verbose=False"),
        },
    )
    found = land.land(harness, "additive", strategy="ff")["restart"]
    assert found["mcp"] == ["src/agent_cli/mcp/specs_dispatch.py", "src/agent_cli/mcp/tools.py"]
    assert found["mcp_moved"] == [] and found["hooks_removed"] == []


def test_a_change_that_touches_none_of_it_warns_nothing(harness: Path):  # noqa: F811
    _base(harness)
    _branch(harness, "plain", {"a.txt": "plain\n"})
    journal = land.land(harness, "plain", strategy="ff")
    assert "restart" not in journal
    assert land_warning.text(journal.get("restart")) == ""


def test_without_live_json_every_manager_is_told_to_restart(harness: Path):  # noqa: F811
    _branch(harness, "state", {"src/agent_cli/dispatch/state.py": "SCHEMA = 2\n"})
    found = land.land(harness, "state", strategy="ff")["restart"]
    assert found["state"] == ["src/agent_cli/dispatch/state.py"] and found["managers"] == []
    assert "restart any manager started before this" in land_warning.text(found)


def _events(root: Path, kind: str) -> list[dict]:
    lines = [
        json.loads(line)
        for p in sorted((root / ".agent" / "telemetry").rglob("*.jsonl"))
        for line in p.read_text().splitlines()
    ]
    return [e for e in lines if e.get("kind") == kind]


def test_every_landing_request_leaves_a_land_op_event(harness: Path):  # noqa: F811
    journal = land.land(harness, "feature", strategy="ff")
    (done,) = _events(harness, "land.op")
    assert done["outcome"] == "done" and done["commit"] == journal["cand"]
    assert done["data"]["record_kind"] == "full" and done["data"]["branch"] == "feature"
    assert done["data"]["lock_wait_s"] >= 0 and done["duration_s"] >= done["data"]["lock_wait_s"]
    (record,) = _events(harness, "verify.record")
    assert record["outcome"] == "passed" and record["data"]["tier"] == "full"
    try:
        land.land(harness, "no-such-branch")
    except Exception:  # noqa: BLE001 — the failure itself is what is recorded
        pass
    failed = [e for e in _events(harness, "land.op") if e["outcome"] == "failed"]
    assert len(failed) == 1 and "no-such-branch" in failed[0]["data"]["why"]
