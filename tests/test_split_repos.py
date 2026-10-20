"""The two-repo split: writes follow symlinks; unknown registry blocks get diagnostics."""

from __future__ import annotations

from pathlib import Path

import yaml

import symlinks
from agent_cli import repos
from agent_cli.core import atomic_write, write_if_changed


def test_atomic_write_writes_through_a_symlink(tmp_path: Path) -> None:
    symlinks.require()
    notes = tmp_path / "notes"
    notes.mkdir()
    target = notes / "STATUS.md"
    target.write_text("old\n", encoding="utf-8")
    link = tmp_path / "STATUS.md"
    link.symlink_to(target)

    atomic_write(link, "new\n")

    assert link.is_symlink(), "the rename must not replace the link with a real file"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_write_if_changed_keeps_the_symlink(tmp_path: Path) -> None:
    symlinks.require()
    target = tmp_path / "real.md"
    target.write_text("a\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    assert write_if_changed(link, "a\n") is False
    assert write_if_changed(link, "b\n") is True
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "b\n"


def _registry(**extra: object) -> dict[str, object]:
    return {
        "version": 1,
        "repos": [{"name": "x", "path": "../repos/x", "remote": "git@example:x.git"}],
        **extra,
    }


def test_registry_diagnoses_unknown_blocks_but_loads_them(tmp_path: Path) -> None:
    """A plan retires silent validation of unknown shared keys.
    Ordinary loads remain compatible with an overlay's extra blocks."""
    data = _registry(tracker={"org": "acme", "project": "Core"})
    assert repos.validate_registry_data(data) == ["agent.yaml: unknown key 'tracker'"]
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert repos.load_registry(tmp_path).require("x").remote == "git@example:x.git"
    assert yaml.safe_load(path.read_text()) == data
