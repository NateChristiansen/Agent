"""Both clients may write in the harness worktree parent.

Harness worktrees at `../Agent-wt-<name>` sat outside both clients' writable grants: every edit
there prompted in Claude Code and Codex refused the write. They now live under one fixed parent,
`../Agent-worktrees/`, and setup grants that parent in per-machine config only: Claude's
gitignored `.claude/settings.local.json` and the generated Codex profile. The checked-in
`.claude/settings.json` asks before editing a few protected paths; those asks match at any depth
only under the cwd, so the local file repeats them, anchored at the filesystem root, for every
worktree under the parent.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from agent_cli import notes, worktree_grants

PROTECTED = (
    "harness/owner/rules.md",
    "harness/voice.md",
    ".claude/settings.json",
    ".mcp.json",
    "bin/agent",
    "src/agent_cli/hooks/**",
)


def _rule_path(path: Path) -> str:
    """The path as Claude's rules spell it, written out here rather than taken from production:
    POSIX form, and on Windows the drive `C:` as `/c`."""
    if sys.platform == "win32":
        text = str(path).replace("\\", "/")
        return "/" + text[0].lower() + text[2:]
    return path.as_posix()


def _settings(root: Path) -> Path:
    path = root / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    ask = ["Bash(git push:*)", *(f"Edit({p})" for p in PROTECTED)]
    path.write_text(json.dumps({"permissions": {"ask": ask}}), encoding="utf-8")
    return path


def _local(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))


def test_the_parent_is_a_sibling_of_the_checkout(tmp_root: Path):
    assert worktree_grants.parent_for(tmp_root) == (tmp_root.parent / "Agent-worktrees").resolve()


def test_settings_local_is_generated_idempotently_and_keeps_the_owners_keys(tmp_root: Path):
    _settings(tmp_root)
    local = tmp_root / ".claude" / "settings.local.json"
    local.write_text(
        json.dumps(
            {
                "enabledMcpjsonServers": ["vendor"],
                "model": "opus",
                "permissions": {
                    "allow": ["Bash(make:*)"],
                    "ask": ["Bash(rm:*)"],
                    "additionalDirectories": ["/elsewhere"],
                },
            }
        ),
        encoding="utf-8",
    )
    assert worktree_grants.configure_claude(tmp_root)[0] == "done"
    first = local.read_text(encoding="utf-8")
    assert worktree_grants.configure_claude(tmp_root)[0] == "already"
    assert local.read_text(encoding="utf-8") == first

    data = _local(tmp_root)
    parent = str(worktree_grants.parent_for(tmp_root))
    assert data["enabledMcpjsonServers"] == ["vendor"]
    assert data["model"] == "opus"
    assert data["permissions"]["allow"] == ["Bash(make:*)", *worktree_grants.allow_rules(Path(parent))]
    assert data["permissions"]["additionalDirectories"] == ["/elsewhere", parent]
    assert data["permissions"]["ask"][0] == "Bash(rm:*)"
    assert data["permissions"]["additionalDirectories"].count(parent) == 1


def test_settings_local_is_created_when_absent(tmp_root: Path):
    _settings(tmp_root)
    assert worktree_grants.configure_claude(tmp_root)[0] == "done"
    parent = str(worktree_grants.parent_for(tmp_root))
    assert _local(tmp_root)["permissions"]["additionalDirectories"] == [parent]


def test_settings_local_that_is_not_json_is_refused_untouched(tmp_root: Path):
    _settings(tmp_root)
    local = tmp_root / ".claude" / "settings.local.json"
    local.write_text("{not json", encoding="utf-8")
    state, detail = worktree_grants.configure_claude(tmp_root)
    assert state == "refused" and "settings.local.json" in detail
    assert local.read_text(encoding="utf-8") == "{not json"


def test_the_generated_ask_rules_cover_each_protected_path_in_every_worktree(tmp_root: Path):
    _settings(tmp_root)
    worktree_grants.configure_claude(tmp_root)
    parent = _rule_path(worktree_grants.parent_for(tmp_root))
    ask = _local(tmp_root)["permissions"]["ask"]
    for rel in PROTECTED:
        assert f"Edit(/{parent}/*/{rel})" in ask
    assert f"Edit(/{parent}/*/src/agent_cli/hooks/**)" in ask
    assert all(rule.startswith("Edit(//") for rule in ask)  # `//` anchors at the filesystem root
    assert not any("git push" in rule for rule in ask)  # only file rules are path-scoped


def test_edit_and_write_are_allowed_under_the_parent_while_protected_paths_still_ask(tmp_root: Path):
    """With only the directory grant, managers were asked before every worktree edit (owner,
    2026-10-04 B0). Ask rules beat allow rules, so the protected paths still ask."""
    _settings(tmp_root)
    worktree_grants.configure_claude(tmp_root)
    parent = _rule_path(worktree_grants.parent_for(tmp_root))
    perms = _local(tmp_root)["permissions"]
    assert perms["allow"] == [f"Edit(/{parent}/**)", f"Write(/{parent}/**)"]
    assert all(rule.startswith(("Edit(//", "Write(//")) for rule in perms["allow"])
    assert f"Edit(/{parent}/*/bin/agent)" in perms["ask"]

    # an allow rule the owner removed by hand is a gap, and setup puts it back
    local = tmp_root / ".claude" / "settings.local.json"
    data = _local(tmp_root)
    data["permissions"]["allow"] = [r for r in data["permissions"]["allow"] if not r.startswith("Write(")]
    local.write_text(json.dumps(data), encoding="utf-8")
    assert worktree_grants.configure_claude(tmp_root)[0] == "done"
    assert f"Write(/{parent}/**)" in _local(tmp_root)["permissions"]["allow"]


def test_the_codex_profile_grants_the_parent(tmp_root: Path):
    parent = str(worktree_grants.parent_for(tmp_root))
    assert notes.configure_codex_notes_root(tmp_root, tmp_root.parent / "notes")[0] == "done"
    config = tomllib.loads((tmp_root / notes.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))
    filesystem = config["permissions"]["agent-manager"]["filesystem"]
    assert filesystem[parent] == "write"
    assert filesystem[str((tmp_root.parent / "notes").resolve())] == "write"

    assert notes.configure_codex_notes_root(tmp_root, None)[0] == "done"  # without linked notes
    config = tomllib.loads((tmp_root / notes.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))
    assert config["permissions"]["agent-manager"]["filesystem"] == {parent: "write"}


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("{not json", "not valid JSON"),
        (json.dumps({"permissions": {"ask": "Edit(bin/agent)"}}), "permissions.ask"),
        (json.dumps({"permissions": {"ask": ["Edit(bin/agent)", 7]}}), "permissions.ask"),
        (json.dumps({"permissions": "ask everything"}), "permissions"),
        (json.dumps(["not", "an", "object"]), "not a settings object"),
    ],
)
def test_an_unusable_source_refuses_the_grant(tmp_root: Path, content: str, why: str):
    """Reviewer probe: with the source rules unreadable, `configure_claude` reported done with
    0 ask rules, so the parent was granted with hooks unguarded."""
    settings = _settings(tmp_root)
    settings.write_text(content, encoding="utf-8")
    state, detail = worktree_grants.configure_claude(tmp_root)
    assert state == "refused" and why in detail
    assert not (tmp_root / ".claude" / "settings.local.json").exists()


def test_an_unreadable_source_refuses_the_grant(tmp_root: Path):
    settings = _settings(tmp_root)
    settings.unlink()
    settings.mkdir()  # exists, cannot be read as a file
    state, detail = worktree_grants.configure_claude(tmp_root)
    assert state == "refused" and "cannot read" in detail
    assert not (tmp_root / ".claude" / "settings.local.json").exists()


def test_an_explicit_empty_ask_list_grants_the_parent_with_no_rules(tmp_root: Path):
    settings = _settings(tmp_root)
    settings.write_text(json.dumps({"permissions": {"ask": []}}), encoding="utf-8")
    state, detail = worktree_grants.configure_claude(tmp_root)
    assert state == "done" and "0 ask rule(s)" in detail
    assert "ask" not in _local(tmp_root)["permissions"]


def _linked_notes(root: Path) -> Path:
    folder = root.parent / "linked-notes"
    (folder / "features").mkdir(parents=True)
    notes.write_notes_root(root, folder)
    try:
        (root / "features").symlink_to(folder / "features", target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip(f"creating a symlink needs Developer Mode or an elevated shell on Windows: {exc}")
    return folder.resolve()


def test_setup_grants_the_linked_notes_root_as_a_working_directory(tmp_root: Path):
    """A Read of `features/` through the link prompted on a fresh clone."""
    from agent_cli.doctor import checks_notes
    from agent_cli.ops import infra

    _settings(tmp_root)
    folder = _linked_notes(tmp_root)
    assert checks_notes.check_notes_repo(tmp_root)[0]["level"] == "warn"

    step = infra._claude_worktree_step(tmp_root)
    assert step["state"] == "done" and str(folder) in step["detail"]
    parent = str(worktree_grants.parent_for(tmp_root))
    assert _local(tmp_root)["permissions"]["additionalDirectories"] == [parent, str(folder)]
    assert checks_notes.check_notes_repo(tmp_root)[0]["level"] == "ok"

    local = tmp_root / ".claude" / "settings.local.json"
    first = local.read_text(encoding="utf-8")
    assert infra._claude_worktree_step(tmp_root)["state"] == "already"
    assert local.read_text(encoding="utf-8") == first


def test_a_malformed_local_file_refuses_the_notes_grant_and_doctor_says_so(tmp_root: Path):
    from agent_cli.doctor import checks_notes

    _settings(tmp_root)
    folder = _linked_notes(tmp_root)
    local = tmp_root / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"permissions": {"additionalDirectories": 123}}), encoding="utf-8")
    state, detail = worktree_grants.configure_claude(tmp_root, folder)
    assert state == "refused" and "permissions.additionalDirectories" in detail
    assert json.loads(local.read_text(encoding="utf-8")) == {"permissions": {"additionalDirectories": 123}}
    row = checks_notes.check_notes_repo(tmp_root)[0]
    assert row["level"] == "warn" and "cannot check the Claude grant" in row["detail"]


def test_no_checked_in_settings_skips_the_claude_grant(tmp_root: Path):
    state, _ = worktree_grants.configure_claude(tmp_root)
    assert state == "skipped"
    assert not (tmp_root / ".claude" / "settings.local.json").exists()
