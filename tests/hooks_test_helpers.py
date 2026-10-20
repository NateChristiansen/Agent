"""Shared helpers for the `tests/test_hooks_*.py` files (the split of the old test_hooks.py,
mirroring `src/agent_cli/hooks/`'s module-per-concern split). Not a test module itself.
"""

import json
import os
import subprocess
from pathlib import Path

from agent_cli import tab_drain


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed argv in a fixture repo
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],  # noqa: S607 — git from PATH, as every fixture
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def manager_worktrees(root: Path, *names: str) -> list[Path]:
    """Make `root` a git checkout and add a real linked worktree per name under the parent
    `protect-generated` grants (`worktree.worktrees_dir`). The hook trusts only a verified
    worktree, so a bare path under the parent is not one."""
    from agent_cli.worktree import worktrees_dir

    if not (root / ".git").exists():
        _git(root, "init", "-q", "--initial-branch=main")
        _git(root, "commit", "-q", "--allow-empty", "-m", "initial")
    out = []
    for name in names:
        path = worktrees_dir(root) / name
        _git(root, "worktree", "add", "-q", "-b", f"agent/{name}", str(path))
        out.append(path)
    return out


def _ctx(r):
    return json.loads(r.out)["hookSpecificOutput"]["additionalContext"]


def _decision(r):
    if not r.out.strip():
        return ""
    return json.loads(r.out)["hookSpecificOutput"]["permissionDecision"]


def _reason(r):
    return json.loads(r.out)["hookSpecificOutput"]["permissionDecisionReason"]


def edit(tool, path):
    return json.dumps({"tool_name": tool, "tool_input": {"file_path": path}})


def bash(cmd):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})


def enable_tab_drain(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)

    class Process:
        pid = 99999

    monkeypatch.setattr(tab_drain.subprocess, "Popen", lambda *a, **kw: Process())
