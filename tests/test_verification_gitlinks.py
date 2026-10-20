"""Opaque gitlinks are accepted only against the immutable dispatch launch tree."""

from __future__ import annotations

import shutil

import pytest

from agent_cli import links
from agent_cli.dispatch import verification as runner
from agent_cli.dispatch import verification_process as procs
from test_verification_runner import build, copied
from verification_fixtures import config, git, start


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    root, worktree, oid = build(tmp_path_factory.mktemp("template"))
    git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{oid},nested")
    git(worktree, "commit", "-m", "gitlink baseline")
    return root, worktree, git(worktree, "rev-parse", "HEAD")


@pytest.fixture
def linked(template, tmp_path):
    """A worktree whose HEAD adds an unpopulated gitlink at `nested`; the sha is that HEAD."""
    return copied(template, tmp_path)


@pytest.mark.parametrize("empty_dir", [False, True])
def test_unchanged_unpopulated_gitlink_verifies(linked, empty_dir):
    updated = linked
    if empty_dir:
        (updated[1] / "nested").mkdir()
    result = runner.wait(start(updated, config("true"), launch_base=updated[2]))
    assert result.evidence is not None and result.evidence.state == "complete"
    assert result.evidence.before is not None and result.evidence.after is not None
    assert result.evidence.before.clean_at(updated[2])
    assert result.evidence.after.clean_at(updated[2])
    assert result.cleanup_confirmed


@pytest.mark.parametrize("change", ["added", "removed", "changed", "mode"])
@pytest.mark.parametrize("committed", [False, True])
def test_changed_gitlinks_refuse_before_commands(linked, change, committed):
    root, worktree, base = linked
    if change == "added":
        git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{base},other")
    elif change == "removed":
        git(worktree, "update-index", "--force-remove", "nested")
    elif change == "changed":
        git(worktree, "update-index", "--cacheinfo", f"160000,{base},nested")
    else:
        git(worktree, "update-index", "--force-remove", "nested")
        (worktree / "nested").write_text("ordinary tracked file")
        git(worktree, "add", "nested")
    if committed:
        git(worktree, "commit", "-m", "change gitlink")
    with pytest.raises(procs.UnsupportedTree):
        runner.observe_tree(worktree, launch_base=base)
    if (change, committed) != ("changed", True):
        return  # the runner refuses through the same observation; one real launch shows it
    updated = root, worktree, git(worktree, "rev-parse", "HEAD")
    result = runner.wait(start(updated, config("touch forbidden"), launch_base=base))
    assert result.lifecycle["reason"] == "unsupported_tree"
    assert not (worktree / "forbidden").exists()


@pytest.mark.parametrize("population", ["file", "clean_repo", "symlink", "parent_symlink"])
def test_populated_or_symlinked_gitlink_refuses(linked, population):
    _, worktree, base = linked
    path = worktree / "nested"
    if population == "symlink":
        links.make(path, worktree, is_dir=True)
    elif population == "parent_symlink":
        git(worktree, "update-index", "--force-remove", "nested")
        git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{base},parent/nested")
        git(worktree, "commit", "-m", "nested gitlink")
        base = git(worktree, "rev-parse", "HEAD")
        links.make(worktree / "parent", worktree, is_dir=True)
    else:
        path.mkdir()
        if population == "clean_repo":
            git(path, "init", "-b", "main")
        else:
            (path / "untracked").write_text("population")
    with pytest.raises(procs.UnsupportedTree):
        runner.observe_tree(worktree, launch_base=base)


@pytest.mark.parametrize("command", ["mkdir -p nested; touch nested/populated", "ln -s . nested"])
def test_population_during_verification_fails_closed(linked, command):
    updated = linked
    result = runner.wait(start(updated, config(command), launch_base=updated[2]))
    assert result.evidence is not None and result.evidence.state != "complete"
    assert result.evidence.after is None
    assert result.lifecycle["reason"] == "unsupported_tree"


@pytest.mark.parametrize("change", ["tracked", "index", "generated", "hidden", "skip"])
def test_other_tree_refusals_remain_with_opaque_gitlink(linked, change):
    _, worktree, base = linked
    if change == "generated":
        (worktree / "adrs").mkdir()
        (worktree / "adrs/README.md").write_text("tracked generated view")
        git(worktree, "add", "adrs/README.md")
        git(worktree, "commit", "-m", "generated view")
        (worktree / "adrs/README.md").write_text("rewritten generated view")
    elif change in {"hidden", "skip"}:
        flag = "--assume-unchanged" if change == "hidden" else "--skip-worktree"
        git(worktree, "update-index", flag, "tracked")
    else:
        (worktree / "tracked").write_text("modified")
        if change == "index":
            git(worktree, "add", "tracked")
    tree = runner.observe_tree(worktree, launch_base=base)
    assert not tree.clean_at(git(worktree, "rev-parse", "HEAD"))


def test_baseline_is_full_immutable_sha_and_not_branch_name(linked):
    _, worktree, base = linked
    for ref in (None, "HEAD", base[:8], "0" * 40):
        with pytest.raises(procs.UnsupportedTree):
            runner.observe_tree(worktree, launch_base=ref)


def test_observer_rechecks_population_after_diff(linked, monkeypatch):
    _, worktree, base = linked
    original = procs.capture

    def populate(argv, *args, **kwargs):
        result = original(argv, *args, **kwargs)
        if "diff" in argv and not (worktree / "nested").exists():
            (worktree / "nested").mkdir()
            (worktree / "nested/new").touch()
        return result

    monkeypatch.setattr(procs, "capture", populate)
    with pytest.raises(procs.UnsupportedTree):
        runner.observe_tree(worktree, launch_base=base)
    shutil.rmtree(worktree / "nested")
