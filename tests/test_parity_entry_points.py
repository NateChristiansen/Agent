"""Ordinary entry points run with no deployment state.

Only provider activity (the fake transport) and the full verify run (a one-line pass command)
are doubled. Nothing here mocks activation, a runtime fence or a source import check, and no
entry point may create `.agent/authority`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mcp import Client

from agent_cli import dispatch, land
from agent_cli.mcp import server as mcp_server
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401
from land_helpers import git, harness  # noqa: F401


def _no_authority(*roots: Path) -> None:
    for root in roots:
        assert not list(root.rglob(".agent/authority")), f"authority state created under {root}"


def test_new_dispatch_and_same_worktree_resume_need_no_deployment(droot: Path, fake_claude):
    question = envelope("question", open_questions=["which port?"])
    fake_claude.scenario([one(question, commit="first attempt"), one(envelope(), commit="after the answer")])
    sent = _new_fast(droot)
    assert sent["state"] == "question", sent.get("error")
    worktree = Path(sent["worktree"])
    first = git(worktree, "rev-parse", "HEAD")
    resumed = dispatch.resume(droot, dispatch_id=sent["id"], answer="use 8080", watch=True)
    assert resumed["state"] == "done", resumed.get("error")
    assert Path(resumed["worktree"]) == worktree and resumed["branch"] == sent["branch"]
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == sent["branch"]
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", first, "HEAD"], cwd=worktree)
    assert ancestry.returncode == 0 and git(worktree, "rev-parse", "HEAD") != first
    assert [c["cwd"] for c in fake_claude.calls()] == [str(worktree), str(worktree)]
    assert not any(key.startswith("authority") for key in resumed)
    _no_authority(droot.parent)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_a_fresh_mcp_tool_child_needs_no_deployment(tmp_root: Path, notes_remote: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    server = mcp_server.build_server(tmp_root, "manager", ["feature_list", "harness"], fresh=True)
    async with Client(server) as client:
        listed = await client.call_tool("feature_list", {})
        assert listed.is_error is False and listed.structured_content["count"] == 1
        migrated = await client.call_tool("harness", {"action": "scratch_path"})
        assert migrated.is_error is False, migrated.content
    _no_authority(tmp_root.parent)


def _runtime_change(root: Path, branch: str, text: str) -> None:
    git(root, "switch", "-q", "-c", branch, "main")
    module = root / "src" / "agent_cli" / "parity_fixture.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"runtime change {branch}")
    git(root, "switch", "-q", "main")


def test_consecutive_runtime_changing_landings_need_no_deployment(harness: Path):
    _runtime_change(harness, "runtime-one", "VALUE = 1\n")
    first = land.land(harness, "runtime-one", strategy="ff")
    assert first["state"] == "done"
    _runtime_change(harness, "runtime-two", "VALUE = 2\n")
    second = land.land(harness, "runtime-two", strategy="ff")
    assert second["state"] == "done"
    # plan.md:86 requires member trailers on main; ff preserves the worker's tree and parents.
    assert git(harness, "rev-parse", "main") == second["cand"]
    assert git(harness, "rev-parse", "main^") == git(harness, "rev-parse", "runtime-two^")
    assert git(harness, "rev-parse", "main^{tree}") == git(harness, "rev-parse", "runtime-two^{tree}")
    assert (harness / "src/agent_cli/parity_fixture.py").read_text() == "VALUE = 2\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=harness, capture_output=True, text=True)
    assert status.stdout == ""
    _no_authority(harness)
