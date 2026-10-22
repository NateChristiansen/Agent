"""Notes paths are judged by where they sit in the vault, not by where a link lands (C).

`features/`, `journal/`, `harness/owner` and `harness/voice.md` are links into the notes repo, and
`relative()` resolves links, so every Claude write to a plan, retro or calibration landed "outside
the harness root" and `protect-generated` asked: 41 prompts on 2026-10-08, about 630 since 10-02.
A hook ask beats the settings allow list. The notes root maps back to the logical path, which then
gets the same generated-file denials as the native rules, through the link or the physical path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_cli import hooks, links
from harness_root import link_notes
from hooks_test_helpers import _decision, _reason, bash, edit


@pytest.fixture(autouse=True)
def not_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test roots live under the system temp directory, which `protect-generated` lets anything
    write. On the owner's machine the notes repo is a sibling of the clone, not in temp, so that
    exemption hid this bug from every earlier test."""
    monkeypatch.setattr("agent_cli.hooks.guards.TMP_PREFIXES", ())


@pytest.fixture
def linked(tmp_root: Path, notes_remote: Path) -> Path:
    owner = tmp_root / "harness" / "owner"
    owner.mkdir(parents=True, exist_ok=True)
    for name in ("rules.md", "calibration.md"):
        (owner / name).write_text("x\n", encoding="utf-8")
    (tmp_root / "harness" / "voice.md").write_text("x\n", encoding="utf-8")
    notes = link_notes(tmp_root, notes_remote)
    assert links.is_link(tmp_root / "harness" / "owner") and links.is_link(tmp_root / "features")
    (notes / "features" / "001-probe").mkdir(parents=True, exist_ok=True)
    return notes


@pytest.fixture
def active_manager(
    tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch, notes_remote: Path, linked: Path
) -> None:
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    assert run(["feature", "new", "notes-owner", "--title", "Notes owner"]).code == 0
    assert run(["feature", "use", "2", "--session-id", "manager"]).code == 0
    assert run(["feature", "use", "2", "--session-id", "codex-mgr"]).code == 0


def _manager_edit(tool: str, path: str) -> str:
    payload = json.loads(edit(tool, path))
    payload["session_id"] = "manager"
    return json.dumps(payload)


def _spellings(root: Path, notes: Path, rel: str) -> list[str]:
    return [rel, str(root / rel), str(notes / rel)]


ALLOWED = ["features/001-probe/plan.md", "features/001-probe/retro.md", "harness/owner/calibration.md"]
GENERATED = [
    "features/001-probe/log.md",
    "features/001-probe/handoffs/mac-01.md",
    "features/001-probe/records/x.json",
    "bases/features.base",
    "bases/x.txt",
    "journal/2026-10-08.collect.json",
]
OWNER_FILES = ["harness/owner/rules.md", "harness/voice.md"]


def test_a_notes_write_through_the_link_or_the_physical_path_is_not_asked(
    tmp_root: Path, linked: Path, run, active_manager
):
    for rel in ALLOWED:
        for path in _spellings(tmp_root, linked, rel):
            r = run(["hook", "protect-generated"], stdin=_manager_edit("Edit", path))
            assert _decision(r) == "", (path, r.out)


def test_generated_notes_files_are_denied_through_every_spelling(tmp_root: Path, linked: Path, run):
    for rel in GENERATED:
        for path in _spellings(tmp_root, linked, rel)[1:]:  # the bare relative one is the native rule's
            r = run(["hook", "protect-generated"], stdin=edit("Write", path))
            assert _decision(r) == "deny", (path, r.out)
            assert "CLI" in _reason(r)


def test_owner_files_are_still_asked(tmp_root: Path, linked: Path, run, active_manager):
    for rel in OWNER_FILES:
        for path in _spellings(tmp_root, linked, rel):
            r = run(["hook", "protect-generated"], stdin=_manager_edit("Edit", path))
            # A file hard-linked into the notes repo (Windows without symlinks) stays inside the
            # root under its in-root spellings, where the native ask rule answers and the hook
            # adds nothing.
            hard_linked = (
                sys.platform == "win32"
                and rel == "harness/voice.md"
                and links.is_hard_linked(tmp_root / rel, linked / rel)
                and path != str(linked / rel)
            )
            assert _decision(r) == ("" if hard_linked else "ask"), (path, r.out)


def test_unlinked_notes_paths_and_outside_paths_keep_the_ask(tmp_root: Path, linked: Path, run):
    for path in (str(linked / "records" / "x.json"), str(linked / "terms.yaml"), "/opt/elsewhere/notes.md"):
        r = run(["hook", "protect-generated"], stdin=edit("Write", path))
        assert _decision(r) == "ask", (path, r.out)
        assert "outside the harness root" in _reason(r)


def _patch(*paths: str) -> str:
    sections = "\n".join(f"*** Update File: {p}\n@@\n-old\n+new" for p in paths)
    return json.dumps(
        {
            "session_id": "codex-mgr",
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": f"*** Begin Patch\n{sections}\n*** End Patch"},
        }
    )


def test_codex_patches_get_the_same_mapping(tmp_root: Path, linked: Path, active_manager):
    for rel in ALLOWED:
        out, _ = hooks.run(tmp_root, "protect-generated", _patch(str(linked / rel)))
        assert out == "" or json.loads(out)["hookSpecificOutput"].get("permissionDecision") != "deny", rel
    for rel in [*GENERATED, *OWNER_FILES]:
        out, _ = hooks.run(tmp_root, "protect-generated", _patch(str(linked / rel)))
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny", rel


def test_a_plan_edit_through_the_link_rerenders_status(tmp_root: Path, notes_remote: Path, run):
    linked = link_notes(tmp_root, notes_remote)
    run(["feature", "new", "x", "--title", "X"])
    status = tmp_root / "Dashboard.md"  # Audit 10 plan.md:113 (K1)
    for path in ("features/001-x/plan.md", str(linked / "features" / "001-x" / "plan.md")):
        status.unlink(missing_ok=True)
        run(["hook", "render-after-plan-edit"], stdin=edit("Edit", path))
        assert status.exists(), path


def test_the_shared_generated_list_matches_the_settings_deny_rules():
    from agent_cli.hooks import NATIVE_GENERATED_DENY, VAULT_GENERATED_DENY

    settings = json.loads((Path(__file__).resolve().parents[1] / ".claude/settings.json").read_text())
    vault = {
        rule
        for rule in settings["permissions"]["deny"]
        if rule.startswith("Edit(/") and not rule.startswith("Edit(/.agent/")
    }
    assert vault == set(VAULT_GENERATED_DENY) | {"Edit(/journal/*.collect.*)"}
    assert set(NATIVE_GENERATED_DENY) >= vault


# ---------------------------------------------------------------- Codex review of phase C


def test_a_hard_linked_owner_file_is_refused_to_codex_by_either_spelling(tmp_root: Path, linked: Path):
    """The Windows fallback links a file entry by hard link, which never resolves to its target."""
    import os

    voice = tmp_root / "harness" / "voice.md"
    voice.unlink()
    os.link(linked / "harness" / "voice.md", voice)
    for path in ("harness/voice.md", str(voice), str(linked / "harness" / "voice.md")):
        out, _ = hooks.run(tmp_root, "protect-generated", _patch(path))
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny", path


def test_root_records_are_protected_for_bash_and_codex_but_src_records_are_not(
    tmp_root: Path, run, active_manager
):
    for cmd in ("echo x > records/x.json", "cp a.json records/x.json", "echo x | tee records/x.json"):
        payload = json.loads(bash(cmd))
        payload["session_id"] = "manager"
        r = run(["hook", "protect-generated"], stdin=json.dumps(payload))
        assert _decision(r) == "deny", cmd
    out, _ = hooks.run(tmp_root, "protect-generated", _patch("records/x.json"))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    for cmd in ("echo x > src/agent_cli/records/narrow.py", "cp a.py src/agent_cli/records/x.py"):
        payload = json.loads(bash(cmd))
        payload["session_id"] = "manager"
        r = run(["hook", "protect-generated"], stdin=json.dumps(payload))
        assert _decision(r) == "", cmd
    out, _ = hooks.run(tmp_root, "protect-generated", _patch("src/agent_cli/records/narrow.py"))
    assert out == "" or json.loads(out)["hookSpecificOutput"]["permissionDecision"] != "deny"


def test_pwd_and_symlinked_records_are_still_root_records(tmp_root: Path, run, tmp_path_factory):
    """`$PWD/records/x` and a `records/` that links outside the vault both lost
    the root spelling the native rule protects."""
    for cmd in ('printf x > "$PWD/records/x.json"', "printf x > ${PWD}/records/x.json"):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert _decision(r) == "deny", cmd
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    links.make(tmp_root / "records", elsewhere, is_dir=True)
    for cmd in ("echo x > records/x.json", f"echo x > {tmp_root}/records/x.json"):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert _decision(r) == "deny", cmd
    for path in ("records/x.json", str(tmp_root / "records" / "x.json")):
        out, _ = hooks.run(tmp_root, "protect-generated", _patch(path))
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny", path


def test_dot_spellings_do_not_hide_a_generated_file_from_bash(tmp_root: Path, linked: Path, run):
    log = linked / "features" / "001-probe" / "log.md"
    log.write_text("x\n", encoding="utf-8")
    for target in (
        str(linked / "features" / "001-probe" / ".." / "001-probe" / "log.md"),
        "features/001-probe/./log.md",
        "journal/./2026-10-08.collect.json",
    ):
        for cmd in (f"echo x > {target}", f"echo x | tee {target}", f"cp a.md {target}"):
            r = run(["hook", "protect-generated"], stdin=bash(cmd))
            assert _decision(r) == "deny", cmd


def test_an_owner_file_respects_another_sessions_lease(tmp_root: Path, linked: Path, run, monkeypatch):
    import os

    from agent_cli import core, leases, sessions

    monkeypatch.setattr(core, "is_alive", lambda pid: True)
    sessions.set_record(tmp_root, "mine", name="mgr-mine")
    leases.take(tmp_root, "vault:harness/owner", session_id="peer", pid=os.getpid())
    payload = json.dumps(
        {"session_id": "mine", "tool_name": "Edit", "tool_input": {"file_path": "harness/owner/rules.md"}}
    )
    r = run(["hook", "protect-generated"], stdin=payload)
    assert _decision(r) == "deny"
