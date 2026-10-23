"""The harness registered as its own repo with `path: .`.

`protect-generated` refuses edits under a registered repo as product code. With the harness
itself registered, that refused every manager edit in the main checkout, so the repo whose path
is the harness root is skipped. Its `worktrees_dir`, where workers run, is still refused.
"""

import json
from pathlib import Path

from agent_cli import hooks
from hooks_test_helpers import _decision, _reason, bash, edit


def _patch(*sections: str) -> str:
    body = "*** Begin Patch\n" + "\n".join(sections) + "\n*** End Patch"
    return json.dumps(
        {
            "session_id": "codex-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": body},
        }
    )


def _register(root: Path, path: str, worktrees_dir: str | None = None) -> None:
    reg = root / "agent.yaml"
    entry = (
        f"  - name: Agent\n    short: agent\n    path: {path}\n    remote: git@github.com:example/Agent.git\n"
    )
    if worktrees_dir:
        entry += f"    worktrees_dir: {worktrees_dir}\n"
    reg.write_text(reg.read_text() + entry)


def test_the_harness_registered_at_its_own_root_is_not_product_code(tmp_root: Path, run):
    _register(tmp_root, ".", "../Agent-worktrees/_dispatch/agent")
    for path in ("src/agent_cli/features.py", str(tmp_root / "tests" / "test_x.py")):
        r = run(["hook", "protect-generated"], stdin=edit("Edit", path))
        assert r.out == "", path
    r = run(["hook", "protect-generated"], stdin=bash(f"cp a.py {tmp_root}/src/agent_cli/b.py"))
    assert r.out == ""

    worker = tmp_root.parent / "Agent-worktrees" / "_dispatch" / "agent" / "022-x-01" / "src" / "a.py"
    r = run(["hook", "protect-generated"], stdin=edit("Edit", str(worker)))
    assert _decision(r) == "deny" and "manager" in _reason(r)
    r = run(["hook", "protect-generated"], stdin=bash(f"cp a.py {worker}"))
    assert _decision(r) == "deny"


def test_codex_apply_patch_gets_the_same_exception(tmp_root: Path):
    """Codex apply_patch reaches `under_registered_repo` through `patch_path_guard`. The
    generated-file refusal still runs for a root file."""
    _register(tmp_root, ".", "../Agent-worktrees/_dispatch/agent")
    out, _ = hooks.run(tmp_root, "protect-generated", _patch("*** Add File: tests/test_x.py\n+text"))
    assert out == ""
    worker = tmp_root.parent / "Agent-worktrees" / "_dispatch" / "agent" / "022-x-01" / "a.py"
    out, _ = hooks.run(tmp_root, "protect-generated", _patch(f"*** Add File: {worker}\n+text"))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny" and "/dispatch" in out
    out, _ = hooks.run(tmp_root, "protect-generated", _patch("*** Update File: STATUS.md\n@@\n-a\n+b"))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_worktrees_dir_at_the_root_is_not_exempt(tmp_root: Path, run):
    """Only `path` is exempt. A worktrees_dir that resolves to the root is a misconfiguration,
    and refusing there fails closed."""
    _register(tmp_root, ".", ".")
    r = run(["hook", "protect-generated"], stdin=edit("Edit", "src/agent_cli/features.py"))
    assert _decision(r) == "deny" and "manager" in _reason(r)


def test_a_repo_at_the_roots_parent_or_a_subfolder_is_still_refused(tmp_root: Path, run):
    _register(tmp_root, "..")
    r = run(["hook", "protect-generated"], stdin=edit("Edit", "src/agent_cli/features.py"))
    assert _decision(r) == "deny" and "manager" in _reason(r)


def test_a_repo_in_a_subfolder_of_the_root_is_still_refused(tmp_root: Path, run):
    _register(tmp_root, "vendor/product")
    r = run(["hook", "protect-generated"], stdin=edit("Edit", str(tmp_root / "vendor" / "product" / "a.py")))
    assert _decision(r) == "deny" and "manager" in _reason(r)
    r = run(["hook", "protect-generated"], stdin=edit("Edit", "src/agent_cli/features.py"))
    assert r.out == ""
