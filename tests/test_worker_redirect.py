"""A worker shell redirect cannot write outside its dispatch worktree."""

import asyncio
from pathlib import Path
from typing import Any, cast

from agent_cli import links
from agent_cli.dispatch.codex_permissions import decision
from agent_cli.dispatch.rules import sdk_hooks
from agent_cli.hooks.paths import PROTECTED_NOTE
from agent_cli.hooks.worker_redirect import outside_redirect
from dispatch_helpers import droot  # noqa: F401 -- pytest fixture


def test_outside_redirect_is_refused(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert outside_redirect("echo x > ~/.ssh/authorized_keys", worktree)
    assert outside_redirect(f"echo x >> {tmp_path / 'outside.txt'}", worktree)
    assert outside_redirect("echo x > ../outside.txt", worktree)
    assert outside_redirect(f"echo x <> {tmp_path / 'read_write.txt'}", worktree)
    assert outside_redirect("echo x >(tee ~/.ssh/authorized_keys)", worktree)
    assert outside_redirect("echo x > >(tee ~/.ssh/authorized_keys)", worktree)


def test_in_worktree_redirect_and_fd_duplication_pass(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert outside_redirect("echo x > output.txt", worktree) is None
    assert outside_redirect("echo x 2>> logs/errors.txt", worktree) is None
    assert outside_redirect("echo x 2>&1", worktree) is None
    assert outside_redirect("echo '>' > output.txt", worktree) is None
    assert outside_redirect("echo x > /dev/null", worktree) is None


def test_symlink_and_dynamic_targets_are_refused(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    links.make(worktree / "escape", tmp_path, is_dir=True)
    assert outside_redirect("echo x > escape/out.txt", worktree)
    assert outside_redirect('echo x > "$HOME/.ssh/authorized_keys"', worktree)
    assert outside_redirect("echo x > ${TARGET}", worktree)


def test_claude_worker_pretooluse_refuses_outside_redirect(tmp_path: Path):
    hooks = sdk_hooks(tmp_path, tmp_path)
    callback = hooks["PreToolUse"][0].hooks[0]
    payload: Any = {"tool_name": "Bash", "tool_input": {"command": "echo x > ~/.ssh/authorized_keys"}}
    result = asyncio.run(callback(payload, None, {"signal": None}))
    out = cast(dict[str, Any], result)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert PROTECTED_NOTE in out["permissionDecisionReason"]
    payload["tool_input"]["command"] = "echo x > /dev/null"
    assert asyncio.run(callback(payload, None, {"signal": None})) == {}


def test_codex_worker_pretooluse_refuses_outside_redirect(droot: Path, tmp_path: Path, monkeypatch):
    from agent_cli.dispatch import codex_permissions

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(
        codex_permissions.state, "load", lambda *_args: {"repo": "alpha", "worktree": str(worktree)}
    )
    assert outside_redirect("echo x > ~/.ssh/authorized_keys", worktree)
    denied, why = decision(
        droot, "eval", {"tool_name": "Bash", "tool_input": {"command": "echo x > ~/.ssh/authorized_keys"}}
    )
    assert denied == "deny"
    assert PROTECTED_NOTE in why
    allowed, _ = decision(
        droot, "eval", {"tool_name": "Bash", "tool_input": {"command": "echo x > /dev/null"}}
    )
    assert allowed == "allow"
