"""Seeded fixtures stay isolated (verify speed, phase 2; Codex's plan review): a copied tree
names only its own paths and remotes, and nothing a test writes reaches the template another test
copies from."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import harness_root
import seeded
from dispatch_helpers import droot  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
from test_ops_cases import (
    _ops_template,  # noqa: F401  # pyright: ignore[reportUnusedImport, reportPrivateUsage]
    externals,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
    ops_root,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
)


def _digest(tree: Path) -> str:
    h = hashlib.sha256()
    for dirpath, dirs, files in sorted(os.walk(tree)):
        dirs.sort()
        for name in sorted(files):
            path = Path(dirpath) / name
            h.update(str(path.relative_to(tree)).encode())
            if not path.is_symlink():
                h.update(path.read_bytes())
    return h.hexdigest()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_a_seeded_copy_names_only_its_own_paths(droot: Path, _droot_template: Path):
    base = droot.parent
    for relative in ("harness/agent.yaml", "harness/.agent/notes-root", "Agent-notes/.git/config"):
        text = (base / relative).read_text(encoding="utf-8")
        assert str(_droot_template) not in text and str(_droot_template.resolve()) not in text, relative
    origin = _git(base / "Agent-notes", "remote", "get-url", "origin")
    assert Path(origin).resolve().is_relative_to(base.resolve())


def test_writes_in_a_copy_never_reach_the_template(droot: Path, _droot_template: Path):
    before = _digest(_droot_template)
    notes = droot.parent / "Agent-notes"
    (notes / "scratch.md").write_text("mine\n", encoding="utf-8")
    _git(notes, "add", "scratch.md")
    _git(notes, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "mine")
    _git(notes, "push", "-q", "origin", "HEAD:refs/heads/isolation-probe")
    (droot / "features" / "001-alpha-work" / "plan.md").write_text("changed\n", encoding="utf-8")
    assert _digest(_droot_template) == before


def test_a_linked_copy_names_only_its_own_notes(ops_root: Path, _ops_template: Path):
    """Codex's phase 0-2 review: the linked twins' records went untested."""
    base = ops_root.parent
    linked = list(base.rglob("notes-linked.json")) + list(base.rglob("notes-root"))
    assert linked, "the ops template links its notes"
    for path in linked:
        text = path.read_text(encoding="utf-8")
        assert str(_ops_template) not in text and str(_ops_template.resolve()) not in text, path


def test_repoint_rewrites_every_written_form_of_the_path(tmp_path: Path):
    old, new = tmp_path / "tpl\\x", tmp_path / "dest\\y"  # a backslash, as in a Windows path
    old.mkdir(parents=True)
    (old / "record.json").write_text(json.dumps({"root": str(old / "notes")}), encoding="utf-8")
    (old / "config").write_text(f'url = "{str(old).replace(chr(92), "/")}/remote"\n', encoding="utf-8")
    seeded.repoint(old, old, new)
    assert json.loads((old / "record.json").read_text(encoding="utf-8"))["root"] == str(new / "notes")
    assert str(new).replace("\\", "/") + "/remote" in (old / "config").read_text(encoding="utf-8")


def test_a_copied_root_fetches_only_its_own_remote(tmp_path: Path):
    """Git stores a backslash in a config value as `\\\\`. A plain replace left a Windows copy's
    clone fetching the template's remote while its registry named the copy's."""
    template, copy = tmp_path / "tpl\\x", tmp_path / "copy\\y"  # a backslash, as in a Windows path
    template.mkdir(parents=True)
    harness_root._build_root(template)  # pyright: ignore[reportPrivateUsage]
    assert "\\\\" in (template / harness_root._REPOINTED[1]).read_text(encoding="utf-8")  # pyright: ignore[reportPrivateUsage]
    shutil.copytree(template, copy, symlinks=True)
    harness_root.repoint_files(copy, template, harness_root._REPOINTED)  # pyright: ignore[reportPrivateUsage]
    bare, template_bare = copy / "alpha-remote.git", template / "alpha-remote.git"
    alpha = copy / "harness" / "repos" / "alpha-api"
    registry = yaml.safe_load((copy / "harness" / "agent.yaml").read_text(encoding="utf-8"))
    assert Path(_git(alpha, "remote", "get-url", "origin")) == bare
    assert Path(registry["repos"][0]["remote"]) == bare

    template_head = _git(template_bare, "rev-parse", "refs/heads/main")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "NEW.md").write_text("upstream\n", encoding="utf-8")
    _git(other, "add", "NEW.md")
    _git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "upstream")
    _git(other, "push", "-q", "origin", "main")
    _git(alpha, "fetch", "-q", "origin")
    assert _git(alpha, "rev-parse", "origin/main") == _git(bare, "rev-parse", "refs/heads/main")
    assert _git(alpha, "rev-parse", "origin/main") != template_head
    assert _git(template_bare, "rev-parse", "refs/heads/main") == template_head


def test_seeding_runs_behind_the_per_test_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Codex's phase 0-2 review: session templates were seeded before the function-scoped guards,
    so a seeding step reached `obsidian open` and kept the caller's Codex identity."""
    monkeypatch.setenv("CODEX_SESSION_ID", "the-caller")
    monkeypatch.setenv("OBSIDIAN_CLI", "/Applications/Obsidian.app/Contents/MacOS/obsidian")
    with seeded.seeding(tmp_path):
        assert "CODEX_SESSION_ID" not in os.environ
        stub = Path(os.environ["OBSIDIAN_CLI"])
        assert stub.parent.name.startswith("seed-guard-") and stub.name == "obsidian"
    assert os.environ["CODEX_SESSION_ID"] == "the-caller"
