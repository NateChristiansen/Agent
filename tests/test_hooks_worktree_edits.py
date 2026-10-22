"""`protect-generated` on edits in manager worktrees under `../Agent-worktrees/`
B0). The hook asked before every one, and a hook ask beats every allow rule, so the owner was
prompted on each worktree edit even with the directory granted and Edit and Write allowed.
"""

import json
from pathlib import Path

import pytest

from agent_cli import features, hooks, leases
from hooks_test_helpers import _decision, _reason, edit, manager_worktrees
from test_patch_lease import _patch


def test_protect_generated_lets_manager_worktree_edits_through(tmp_root: Path, run, monkeypatch):
    """Inside a worktree the CLI-owned files are still refused; outside the parent, and in a
    look-alike sibling, it still asks."""
    from agent_cli import worktree_grants
    from agent_cli.hooks import guards

    # tmp_root lives in the temp dir, which the hook lets through; this test is about the rest
    monkeypatch.setattr(guards, "TMP_PREFIXES", ())
    (wt,) = manager_worktrees(tmp_root, "022-x")  # the hook trusts only a verified worktree
    parent = worktree_grants.parent_for(tmp_root)
    for tool, path in (("Edit", "features/001-x/plan.md"), ("Write", ".agent/scratch/draft.md")):
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
    r = run(["hook", "protect-generated"], stdin=edit("Edit", str(mine / ".agent/scratch/a.py")))
    assert r.out == ""


@pytest.fixture
def edit_owner(tmp_root, run, notes_remote, monkeypatch):
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    run(["feature", "new", "edit-owner", "--title", "Edit owner"])
    run(["feature", "new", "other", "--title", "Other"])
    run(["feature", "use", "2", "--session-id", "manager"])
    (wt,) = manager_worktrees(tmp_root, "edit-owner")
    (tmp_root / ".agent/manager-worktrees.json").write_text(
        json.dumps({"agent/edit-owner": {"feature": 1, "session": "manager", "at": "now"}})
    )
    return wt


def _edit_payload(adapter, target):
    if adapter == "apply_patch":
        return _patch("manager", f"*** Add File: {target}\n+text")
    key = "notebook_path" if adapter == "NotebookEdit" else "file_path"
    return {"session_id": "manager", "tool_name": adapter, "tool_input": {key: str(target)}}


@pytest.mark.parametrize("adapter", ["Edit", "Write", "NotebookEdit", "apply_patch"])
def test_bound_feature_wins_over_active_mapping_then_allows_activation(tmp_root, edit_owner, adapter):
    owner = features.resolve(tmp_root, "1")
    payload = _edit_payload(adapter, edit_owner / "src/new.py")
    for status in ("proposed", "blocked", "active"):
        owner.meta["status"] = status
        owner.save()
        out, code = hooks.run(tmp_root, "protect-generated", json.dumps(payload))
        assert code == 0
        if status == "active":
            assert out == ""
        else:
            reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
            assert f"is {status}" in reason
            assert "feature_use 1" in reason and "worktree_new" in reason and "feature=1" in reason
        assert leases.list_all(tmp_root) == []


@pytest.mark.parametrize("adapter", ["Edit", "apply_patch"])
def test_unbound_edits_and_scratch_for_both_clients(tmp_root, edit_owner, adapter):
    (tmp_root / ".agent/manager-worktrees.json").write_text("{}")
    for target in (edit_owner / "src/new.py", tmp_root / "src/new.py"):
        payload = _edit_payload(adapter, target)
        payload["session_id"] = "unmapped"
        out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(payload))
        assert '"permissionDecision": "deny"' in out and "no feature owns" in out
    for target in (
        edit_owner / "features/001-x/plan.md",
        edit_owner / ".agent/scratch/draft.md",
        tmp_root / "features/001-x/design.md",
        tmp_root / ".agent/scratch/draft.md",
        tmp_root.parent / "outside-tmp.txt",
    ):
        payload = _edit_payload(adapter, target)
        payload["session_id"] = "unmapped"
        out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(payload))
        assert out == "", (target, out)
    payload = _edit_payload(adapter, edit_owner / "src/new.py")
    out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(payload))
    assert out == ""  # an unbound branch falls back to the active session mapping


@pytest.mark.parametrize("adapter", ["Edit", "apply_patch"])
@pytest.mark.parametrize(
    "rel", ["src/x.py", "harness/x.md", "tests/x.py", "bin/x", ".claude/x", ".codex/x", ".agents/x"]
)
def test_vault_guarded_roots_need_active_owner(tmp_root, edit_owner, adapter, rel):
    owner = features.resolve(tmp_root, "2")
    owner.meta["status"] = "proposed"
    owner.save()
    payload = _edit_payload(adapter, tmp_root / rel)
    out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(payload))
    assert '"permissionDecision": "deny"' in out and "is proposed" in out
    owner.meta["status"] = "active"
    owner.save()
    out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(payload))
    assert out == ""


def test_patch_checks_all_activity_targets_before_taking_leases(tmp_root, edit_owner):
    owner = features.resolve(tmp_root, "1")
    owner.meta["status"] = "proposed"
    owner.save()
    payload = _patch(
        "manager",
        "*** Add File: src/free.py\n+text",
        f"*** Add File: {edit_owner / 'src/held.py'}\n+text",
    )
    out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(payload))
    assert '"permissionDecision": "deny"' in out and "is proposed" in out
    assert leases.list_all(tmp_root) == []


@pytest.mark.parametrize("group", ["pre-bash", "codex-pre-bash"])
def test_supported_shell_targets_and_reads(tmp_root, edit_owner, group):
    owner = features.resolve(tmp_root, "1")
    owner.meta["status"] = "proposed"
    owner.save()
    for command in (
        f"cp /tmp/input '{edit_owner}/src/x.py'",
        f"echo text > '{edit_owner}/src/x.py'",
        f"touch '{edit_owner}/new.md'",
        "echo text >> src/x.py",
        "printf text | tee tests/x.py",
        "dd if=/tmp/input of=bin/x",
    ):
        payload = {"session_id": "manager", "tool_name": "Bash", "tool_input": {"command": command}}
        # Vault targets use the active session's feature, not the sibling worktree's binding.
        mapped = features.resolve(tmp_root, "2")
        mapped.meta["status"] = "proposed"
        mapped.save()
        out, code = hooks.run(tmp_root, group, json.dumps(payload))
        assert code == 0 and '"permissionDecision": "deny"' in out and "is proposed" in out
        owner.meta["status"] = mapped.meta["status"] = "active"
        owner.save()
        mapped.save()
        out, _ = hooks.run(tmp_root, group, json.dumps(payload))
        assert out == "", (command, out)
        owner.meta["status"] = "proposed"
        owner.save()
    for command in (
        f"cat '{edit_owner}/src/x.py'",
        f"cp /tmp/input '{edit_owner}/.agent/scratch/draft'",
        "echo text > .agent/scratch/draft",
        "echo text > features/001-edit-owner/plan.md",
        "echo text > /tmp/activity-probe",
        "python -c \"open('src/x.py', 'w').write('text')\"",
        "git apply /tmp/probe.patch",
        "ruff format src/x.py",
    ):
        payload = {"session_id": "unmapped", "tool_name": "Bash", "tool_input": {"command": command}}
        out, _ = hooks.run(tmp_root, group, json.dumps(payload))
        assert out == "", (command, out)


@pytest.mark.parametrize("adapter", ["Edit", "apply_patch"])
def test_worker_subagent_exemptions_and_activity_failure(tmp_root, edit_owner, adapter, monkeypatch, capsys):
    from agent_cli import activity_gate, sessions

    owner = features.resolve(tmp_root, "1")
    owner.meta["status"] = "proposed"
    owner.save()
    payload = _edit_payload(adapter, edit_owner / "src/new.py")
    for field in ("agent_id", "agent_type"):
        out, _ = hooks.run(tmp_root, "protect-generated", json.dumps({**payload, field: "subagent"}))
        assert out == ""
    monkeypatch.setenv("AGENT_DISPATCH_ID", "worker")
    assert hooks.run(tmp_root, "protect-generated", json.dumps(payload)) == ("", 0)
    monkeypatch.delenv("AGENT_DISPATCH_ID")
    sessions.set_record(tmp_root, "worker", dispatch_id="worker")
    assert hooks.run(tmp_root, "protect-generated", json.dumps({**payload, "session_id": "worker"})) == (
        "",
        0,
    )

    def broken(*args, **kwargs):
        raise RuntimeError("probe failure")

    monkeypatch.setattr(activity_gate, "edit_refusal", broken)
    assert hooks.run(tmp_root, "protect-generated", json.dumps(payload)) == ("", 0)
    assert "activity check failed (probe failure); allowing the edit" in capsys.readouterr().err
