"""Doctor's row for each product clone's pre-push code-review hook (ADR-0063)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cli import gitgate
from agent_cli.doctor import checks_repos

ROW = "repo alpha pre-push review hook"


def _stale(hook: Path, _root: Path) -> None:
    hook.write_text(f"#!/bin/sh\n{gitgate.MARKER} {gitgate.REVIEW_HOOK}\nold\n", encoding="utf-8")


def _harness_hook(hook: Path, _root: Path) -> None:
    hook.write_text(gitgate.script("pre-push"), encoding="utf-8")


def _hooks_path(_hook: Path, root: Path) -> None:
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"], cwd=root / "repos" / "alpha-api", check=True
    )


def _installed(_hook: Path, root: Path) -> None:
    gitgate.install_review(root)


@pytest.mark.parametrize(
    ("arrange", "level", "says"),
    [
        (_installed, "ok", "installed"),
        (None, "fail", "skip the code-review gate; run `agent setup`"),
        (_stale, "fail", "out of date; run `agent setup`"),
        (_harness_hook, "fail", "left in place; the code-review gate does not run there"),
        (_hooks_path, "fail", "left in place; the code-review gate does not run there"),
    ],
    ids=["installed", "missing", "stale", "foreign", "hooks-path"],
)
def test_each_cloned_repo_reports_its_review_hook_and_an_uncloned_one_has_none(
    tmp_root: Path, arrange, level: str, says: str
) -> None:
    hook = gitgate.hooks_dir(tmp_root / "repos" / "alpha-api") / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    if arrange:
        arrange(hook, tmp_root)
    before = hook.read_bytes() if hook.exists() else None
    rows = {c["check"]: c for c in checks_repos.check_registry(tmp_root)}
    assert (rows[ROW]["level"], rows[ROW]["detail"].endswith(says)) == (level, True)
    assert not any("beta pre-push" in name for name in rows)
    assert (hook.read_bytes() if hook.exists() else None) == before


def test_a_clone_sharing_the_harness_s_hooks_path_still_has_its_row(tmp_root: Path, tmp_path: Path) -> None:
    """Only the harness's own repository is skipped; a shared `core.hooksPath` is not the same
    repository."""
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_root, check=True)
    for checkout in (tmp_root, tmp_root / "repos" / "alpha-api"):
        subprocess.run(["git", "config", "core.hooksPath", str(shared)], cwd=checkout, check=True)
    rows = {c["check"]: c for c in checks_repos.check_registry(tmp_root)}
    assert rows[ROW]["level"] == "fail" and "the code-review gate does not run there" in rows[ROW]["detail"]


def test_an_unreadable_registry_adds_no_hook_row(tmp_root: Path) -> None:
    (tmp_root / "agent.yaml").write_text("repos: [\n", encoding="utf-8")
    rows = checks_repos.check_registry(tmp_root)
    assert [c["level"] for c in rows if c["check"] == "agent.yaml"] == ["fail"]
    assert not any("pre-push" in c["check"] for c in rows)
