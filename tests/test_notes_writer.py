"""Workers links name the configured vault, and only
the main checkout that owns the shared notes rewrites handoff fields on render."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cli import links, notes_writer, status
from agent_cli.core import AgentError
from agent_cli.dispatch import opener


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A main harness checkout `Agent`, a linked worktree of it, a separate probe clone, and one
    notes repo all three are linked to."""
    empty = tmp_path / "gitconfig"
    empty.write_text("[user]\n\tname = t\n\temail = t@t\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    main = tmp_path / "Agent"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "README.md").write_text("x\n", encoding="utf-8")
    git(main, "add", "README.md")
    git(main, "commit", "-q", "-m", "seed")
    worktree = tmp_path / "Agent-worktrees" / "045-win-fixture-acl"
    git(main, "worktree", "add", "-q", "-b", "wt", str(worktree))
    probe = tmp_path / "041-probe"
    git(tmp_path, "clone", "-q", str(main), str(probe))
    notes = tmp_path / "Agent-notes"
    notes.mkdir()
    git(notes, "init", "-q", "-b", "main")
    for root in (main, worktree, probe):
        (root / ".agent").mkdir(exist_ok=True)
        (root / ".agent" / "notes-root").write_text(str(notes), encoding="utf-8")
    # setup's machine-local file, in the real checkout only (`harness_role.create_local`)
    (main / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    return {"main": main, "worktree": worktree, "probe": probe, "notes": notes}


def test_the_link_names_the_main_checkout_from_a_worktree(layout: dict[str, Path]):
    for root in (layout["main"], layout["worktree"]):
        assert opener.vault_name(root) == "Agent"
        assert "vault=Agent&" in opener.open_uri(root, "002-mac-09")


def test_a_configured_vault_name_wins(layout: dict[str, Path]):
    (layout["main"] / "agent.local.yaml").write_text("obsidian_vault: My Vault\n", encoding="utf-8")
    assert opener.vault_name(layout["worktree"]) == "My Vault"
    assert "vault=My%20Vault&" in opener.open_uri(layout["worktree"], "002-mac-09")


def test_only_the_set_up_main_checkout_writes_shared_notes(layout: dict[str, Path]):
    main, worktree, probe = layout["main"], layout["worktree"], layout["probe"]
    assert not notes_writer.writes_shared_notes(worktree)  # a worktree never
    assert notes_writer.writes_shared_notes(main)
    assert not notes_writer.writes_shared_notes(probe)  # a clone with a notes link, no local file


def test_a_probe_that_renders_first_cannot_take_the_role(layout: dict[str, Path]):
    """With a first-claim marker the probe won for good."""
    assert not notes_writer.writes_shared_notes(layout["probe"])
    assert notes_writer.writes_shared_notes(layout["main"])
    assert not notes_writer.writes_shared_notes(layout["probe"])


def test_a_moved_main_checkout_keeps_writing(layout: dict[str, Path], tmp_path: Path):
    assert notes_writer.writes_shared_notes(layout["main"])
    moved = tmp_path / "Agent-moved"
    layout["main"].rename(moved)
    assert notes_writer.writes_shared_notes(moved)
    assert not notes_writer.writes_shared_notes(layout["probe"])


def test_a_fresh_clone_without_a_local_file_never_writes(layout: dict[str, Path], tmp_path: Path):
    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", "-q", str(layout["main"]), str(fresh))
    (fresh / ".agent").mkdir()
    (fresh / ".agent" / "notes-root").write_text(str(layout["notes"]), encoding="utf-8")
    assert not (fresh / "agent.local.yaml").exists()  # git never carries it
    for _ in range(2):
        assert not notes_writer.writes_shared_notes(fresh)


def test_a_copied_checkout_can_opt_out(layout: dict[str, Path]):
    (layout["probe"] / "agent.local.yaml").write_text("machine: mac\nnotes_writer: false\n", encoding="utf-8")
    assert not notes_writer.writes_shared_notes(layout["probe"])
    (layout["probe"] / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    assert notes_writer.writes_shared_notes(layout["probe"])  # a set-up checkout writes


def test_a_render_from_a_non_writer_leaves_handoffs_alone(
    tmp_root: Path, notes_remote, monkeypatch: pytest.MonkeyPatch
):
    """Renders from worktrees and probe clones rewrote every handoff's `open:` link."""
    from agent_cli.dispatch import handoffs

    synced: list[Path] = []
    monkeypatch.setattr(handoffs, "sync_live_fields", lambda root: synced.append(root))
    local = tmp_root / "agent.local.yaml"
    text = local.read_text(encoding="utf-8")
    local.unlink()
    status.render(tmp_root)
    assert synced == []
    local.write_text(text, encoding="utf-8")
    status.render(tmp_root)
    assert synced == [tmp_root]


def test_only_notes_writer_can_publish_orders(layout: dict[str, Path]):
    relative = "features/001-ordinary/orders/001-mac-01.md"
    (layout["notes"] / "features").mkdir()
    for name in ("main", "worktree", "probe"):
        links.make(layout[name] / "features", layout["notes"] / "features", is_dir=True)
    notes_writer.write_work_order(layout["main"], relative, "ordinary composed order\n")
    target = layout["notes"] / relative
    assert target.read_text() == "ordinary composed order\n"
    for name in ("worktree", "probe"):
        with pytest.raises(AgentError, match="notes writer"):
            notes_writer.write_work_order(layout[name], relative, "changed order\n")
    assert target.read_text() == "ordinary composed order\n"
