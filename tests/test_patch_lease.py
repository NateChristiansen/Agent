"""Codex apply_patch edits respect the same vault leases as Claude Edit/Write.

On 2026-09-24 a Codex manager edited hooks/activity.py and dispatch/notify.py through
apply_patch while other managers held those areas. The syntax hook ran; no lease hook did.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_cli import core, hooks, leases, links, sessions
from agent_cli.hooks.patch_lease import patch_lease_guard, patch_paths, protect_with_patch_leases


def _patch(session_id: str, *sections: str) -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Begin Patch\n" + "\n".join(sections) + "\n*** End Patch"},
    }


def test_codex_apply_patch_matcher_runs_lease_guard():
    from agent_cli.hooks.groups import wired

    config = json.loads((Path(__file__).resolve().parents[1] / ".codex/hooks.json").read_text())
    assert wired(config, "PreToolUse", "apply_patch") == ["protect-generated"]
    assert hooks.HANDLERS["protect-generated"] is protect_with_patch_leases


def test_patch_parses_updates_adds_deletes_and_move_targets(tmp_root: Path):
    patch = _patch(
        "mine",
        "*** Update File: src/agent_cli/hooks/activity.py\n@@\n-old\n+new",
        "*** Add File: tests/new.py\n+text",
        "*** Delete File: harness/old.md",
        "*** Update File: harness/name.md\n*** Move to: harness/new.md\n@@\n-old\n+new",
    )["tool_input"]["command"]
    assert patch_paths(tmp_root, patch) == [
        "src/agent_cli/hooks/activity.py",
        "tests/new.py",
        "harness/old.md",
        "harness/name.md",
        "harness/new.md",
    ]


def test_apply_patch_is_denied_when_another_live_manager_holds_the_path(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    sessions.set_record(tmp_root, "mine", name="mgr-032-codex")
    held = "vault:src/agent_cli/hooks"
    leases.take(tmp_root, held, session_id="peer", pid=os.getpid())
    patch = _patch("mine", "*** Update File: src/agent_cli/hooks/activity.py\n@@\n-old\n+new")

    out, code = hooks.run(tmp_root, "protect-generated", json.dumps(patch))
    decision = json.loads(out)["hookSpecificOutput"]
    assert code == 0 and decision["permissionDecision"] == "deny"
    assert held in decision["permissionDecisionReason"]
    lease = leases.get(tmp_root, held)
    assert lease is not None and [r["session_id"] for r in lease["requests"]] == ["mine"]


def test_denied_multifile_patch_does_not_take_earlier_free_path(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    leases.take(tmp_root, "vault:src/agent_cli/dispatch", session_id="peer", pid=os.getpid())
    patch = _patch(
        "mine",
        "*** Add File: src/agent_cli/hooks/new.py\n+text",
        "*** Update File: src/agent_cli/dispatch/notify.py\n@@\n-old\n+new",
    )
    decision = json.loads(patch_lease_guard(tmp_root, patch))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert leases.get(tmp_root, "vault:src/agent_cli/hooks") is None


def test_exact_vault_lease_outside_standard_areas_also_blocks_patch(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    rel = "features/032-example/plan.md"
    leases.take(tmp_root, f"vault:{rel}", session_id="peer", pid=os.getpid())
    patch = _patch("mine", f"*** Update File: {rel}\n@@\n-old\n+new")
    decision = json.loads(patch_lease_guard(tmp_root, patch))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert f"vault:{rel}" in decision["permissionDecisionReason"]


def _decision(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else ""


def test_apply_patch_to_generated_files_is_denied(tmp_root: Path):
    """Claude's native Edit(...) denies never reach Codex, and the apply_patch
    branch ran only the lease check, so a Codex patch could hand-write STATUS.md or a log."""
    for section in (
        "*** Update File: STATUS.md\n@@\n-old\n+new",
        "*** Update File: features/034-harness-cleanup/log.md\n@@\n-old\n+new",
        "*** Add File: features/034-harness-cleanup/handoffs/034-99.md\n+text",
        "*** Update File: bases/features.base\n@@\n-old\n+new",
        "*** Update File: .agent/sessions/abc.json\n@@\n-old\n+new",
        "*** Update File: harness/note.md\n*** Move to: Dashboard.md\n@@\n-old\n+new",
    ):
        out, code = hooks.run(tmp_root, "protect-generated", json.dumps(_patch("mine", section)))
        assert code == 0 and _decision(out) == "deny", section
        assert "status_render" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_apply_patch_to_a_log_behind_a_symlinked_features_dir_is_denied(tmp_root: Path):
    """A linked vault's `features/` points into the notes repo; resolving the path left the
    root and skipped the check (found in review)."""
    notes = tmp_root.parent / "notes-repo" / "features"
    (notes / "034-x").mkdir(parents=True)
    features = tmp_root / "features"
    if features.exists():
        import shutil

        shutil.rmtree(features)
    links.make(features, notes, is_dir=True)
    for target in ("features/034-x/log.md", str(notes / "034-x" / "log.md")):
        patch = _patch("mine", f"*** Update File: {target}\n@@\n-old\n+new")
        out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(patch))
        assert _decision(out) == "deny", target
    plan = _patch("mine", "*** Update File: features/034-x/plan.md\n@@\n-old\n+new")
    out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(plan))
    assert _decision(out) != "deny"


def test_apply_patch_to_hand_editable_files_is_allowed(tmp_root: Path):
    for section in (
        "*** Update File: features/034-harness-cleanup/plan.md\n@@\n-old\n+new",
        "*** Add File: .agent/scratch/plans/draft.md\n+text",
        "*** Update File: journal/2026/09-September/19-Saturday.md\n@@\n-old\n+new",
    ):
        out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(_patch("mine", section)))
        assert _decision(out) != "deny", section


def test_apply_patch_into_a_registered_product_repo_is_denied(tmp_root: Path):
    sibling = tmp_root.parent / "sibling" / "Gamma"
    sibling.mkdir(parents=True)
    reg = tmp_root / "agent.yaml"
    reg.write_text(
        reg.read_text()
        + "  - name: Gamma\n    short: gamma\n    path: ../sibling/Gamma\n    remote: git@github.com:example/Gamma.git\n"
    )
    patch = _patch("mine", f"*** Update File: {sibling / 'src' / 'x.py'}\n@@\n-old\n+new")
    out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(patch))
    assert _decision(out) == "deny"
    assert "/dispatch" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_denied_generated_patch_takes_no_lease(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    patch = _patch(
        "mine",
        "*** Add File: src/agent_cli/hooks/new.py\n+text",
        "*** Update File: STATUS.md\n@@\n-old\n+new",
    )
    out, _ = hooks.run(tmp_root, "protect-generated", json.dumps(patch))
    assert _decision(out) == "deny"
    assert leases.get(tmp_root, "vault:src/agent_cli/hooks") is None


def test_own_file_lease_allows_patch_beside_another_manager_file(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    leases.take(tmp_root, "vault:src/agent_cli/hooks/activity.py", session_id="peer", pid=os.getpid())
    rel = "src/agent_cli/hooks/new.py"
    leases.take(tmp_root, f"vault:{rel}", session_id="mine", pid=os.getpid())
    patch = _patch("mine", f"*** Add File: {rel}\n+text")
    assert patch_lease_guard(tmp_root, patch) == ""
    lease = leases.get(tmp_root, f"vault:{rel}")
    assert lease is not None and lease["session_id"] == "mine"
