"""`protect-generated` on edits in manager worktrees under `../Agent-worktrees/`
B0). The hook asked before every one, and a hook ask beats every allow rule, so the owner was
prompted on each worktree edit even with the directory granted and Edit and Write allowed.
"""

from pathlib import Path

from hooks_test_helpers import _decision, _reason, edit, manager_worktrees


def test_protect_generated_lets_manager_worktree_edits_through(tmp_root: Path, run, monkeypatch):
    """Inside a worktree the CLI-owned files are still refused; outside the parent, and in a
    look-alike sibling, it still asks."""
    from agent_cli import worktree_grants
    from agent_cli.hooks import guards

    # tmp_root lives in the temp dir, which the hook lets through; this test is about the rest
    monkeypatch.setattr(guards, "TMP_PREFIXES", ())
    (wt,) = manager_worktrees(tmp_root, "022-x")  # the hook trusts only a verified worktree
    parent = worktree_grants.parent_for(tmp_root)
    for tool, path in (("Edit", "src/agent_cli/features.py"), ("Write", "tests/test_new.py")):
        r = run(["hook", "protect-generated"], stdin=edit(tool, str(wt / path)))
        assert r.out == "", (tool, path)
    for path in ("STATUS.md", "features/003-x/log.md", ".agent/sessions/abc.json", "adrs/README.md"):
        r = run(["hook", "protect-generated"], stdin=edit("Write", str(wt / path)))
        assert _decision(r) == "deny" and "CLI-owned" in _reason(r), path
    r = run(["hook", "protect-generated"], stdin=edit("Write", str(wt / "repos" / "x.py")))
    assert _decision(r) == "deny" and "manager" in _reason(r)
    for outside in (parent.parent / f"{parent.name}-evil" / "022-x" / "a.py", parent / "loose.md"):
        r = run(["hook", "protect-generated"], stdin=edit("Write", str(outside)))
        assert _decision(r) == "ask" and "outside the harness root" in _reason(r), outside


def test_a_worker_worktree_under_the_parent_is_still_refused(tmp_root: Path, run, monkeypatch):
    """Phase B registers this harness with its integration worktree as the repo path and
    `worktrees_dir` under the same parent. AGENTS.md: never edit a worker worktree. The
    registered-repo check runs before the worktree branch, so both stay refused."""
    from agent_cli import worktree_grants
    from agent_cli.hooks import guards

    monkeypatch.setattr(guards, "TMP_PREFIXES", ())
    (mine,) = manager_worktrees(tmp_root, "022-mine")  # the hook trusts only a verified worktree
    parent = worktree_grants.parent_for(tmp_root)
    reg = tmp_root / "agent.yaml"
    reg.write_text(
        reg.read_text()
        + "  - name: Harness\n    short: harness\n    path: ../Agent-worktrees/022-integration\n"
        + "    worktrees_dir: ../Agent-worktrees/_dispatch\n    remote: git@github.com:example/Agent.git\n"
    )
    for path in (parent / "_dispatch" / "022-x-01" / "src" / "a.py", parent / "022-integration" / "a.py"):
        r = run(["hook", "protect-generated"], stdin=edit("Edit", str(path)))
        assert _decision(r) == "deny" and "manager" in _reason(r), path
    r = run(["hook", "protect-generated"], stdin=edit("Edit", str(mine / "a.py")))
    assert r.out == ""
