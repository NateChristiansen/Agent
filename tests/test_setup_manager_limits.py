"""Setup writes the owner-approved manager settings.

Codex gets no compaction limit: its usable window is about 258k, so the model default applies
(2026-10-05). An owner-set limit survives setup. Claude may edit and write under the notes
repo's `features/` and `adrs/` without a prompt.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from agent_cli import codex_profile, worktree_grants

LIMIT = "model_auto_compact_token_limit"
SCOPE = "model_auto_compact_token_limit_scope"


def _codex(root: Path) -> str:
    return (root / ".codex" / "config.toml").read_text(encoding="utf-8")


def test_codex_gets_no_compaction_limit(tmp_root: Path):
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "done"
    data = tomllib.loads(_codex(tmp_root))
    assert LIMIT not in data
    assert SCOPE not in data
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "already"


def test_an_owner_set_codex_limit_survives_setup(tmp_root: Path):
    path = tmp_root / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'model = "x"\n{LIMIT} = 150000\n\n[other]\na = 1\n', encoding="utf-8")
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "done"
    data = tomllib.loads(_codex(tmp_root))
    assert data[LIMIT] == 150000
    assert SCOPE not in data
    assert data["model"] == "x"
    assert data["other"] == {"a": 1}
    assert _codex(tmp_root).count(LIMIT + " =") == 1
    assert codex_profile.configure_codex_notes_root(tmp_root)[0] == "already"


def _claude_settings(root: Path) -> None:
    path = root / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"ask": []}}), encoding="utf-8")


def _allow(root: Path) -> list[str]:
    data = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    return data["permissions"]["allow"]


def test_claude_allows_use_the_resolved_notes_root(tmp_root: Path, tmp_path: Path):
    _claude_settings(tmp_root)
    notes = (tmp_path / "notes-repo").resolve()
    assert worktree_grants.configure_claude(tmp_root, notes)[0] == "done"
    if sys.platform == "win32":
        text = str(notes).replace("\\", "/")
        base = f"//{text[0].lower()}{text[2:]}"
    else:
        base = f"/{notes.as_posix()}"
    allow = _allow(tmp_root)
    for tool in ("Edit", "Write"):
        for sub in ("features", "adrs"):
            assert f"{tool}({base}/{sub}/**)" in allow


def test_claude_notes_allows_are_idempotent_and_keep_existing_entries(tmp_root: Path, tmp_path: Path):
    _claude_settings(tmp_root)
    local = tmp_root / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"permissions": {"allow": ["Bash(make:*)"]}}), encoding="utf-8")
    notes = (tmp_path / "notes-repo").resolve()
    worktree_grants.configure_claude(tmp_root, notes)
    first = local.read_text(encoding="utf-8")
    assert worktree_grants.configure_claude(tmp_root, notes)[0] == "already"
    assert local.read_text(encoding="utf-8") == first
    allow = _allow(tmp_root)
    assert allow[0] == "Bash(make:*)"
    assert len(allow) == len(set(allow))


def test_no_notes_root_adds_no_notes_allows(tmp_root: Path):
    _claude_settings(tmp_root)
    worktree_grants.configure_claude(tmp_root)
    assert not any("features" in rule or "adrs" in rule for rule in _allow(tmp_root))


def test_the_codex_manager_profile_grants_the_notes_remote(tmp_root, tmp_path, monkeypatch) -> None:
    """A Codex manager's feature_new could not reach the notes remote. The host
    and, for SSH, the agent socket are granted; nothing else is widened."""
    import subprocess
    import tomllib

    notes = tmp_path / "notes"
    subprocess.run(["git", "init", "-q", str(notes)], check=True)
    subprocess.run(
        ["git", "-C", str(notes), "remote", "add", "origin", "git@github.com:owner/notes.git"], check=True
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/var/run/launchd.test/Listeners")
    assert codex_profile.configure_codex_notes_root(tmp_root, notes)[0] == "done"
    config = tomllib.loads((tmp_root / codex_profile.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))
    network = config["permissions"]["agent-manager"]["network"]
    assert network["domains"] == {"api.github.com": "allow", "github.com": "allow"}
    assert network["unix_sockets"] == {
        "/tmp/cc-socks": "allow",
        "/private/var/run/launchd.test/Listeners": "allow",
    }
    subprocess.run(
        ["git", "-C", str(notes), "remote", "set-url", "origin", "https://git.example.org/notes.git"],
        check=True,
    )
    assert codex_profile.configure_codex_notes_root(tmp_root, notes)[0] == "done"
    config = tomllib.loads((tmp_root / codex_profile.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))
    network = config["permissions"]["agent-manager"]["network"]
    assert network["domains"] == {"api.github.com": "allow", "git.example.org": "allow"}
    assert network["unix_sockets"] == {"/tmp/cc-socks": "allow"}
