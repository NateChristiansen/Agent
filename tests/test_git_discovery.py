"""`main_checkout` without starting git agrees with git.

The Python discovery must keep git's behaviour: ancestor discovery from a nested path, linked
worktrees through their gitfile and `commondir`, ceiling directories, an error outside any
repository, and no memory of earlier answers (a path that was not a repository, or that now
belongs to another repository, is read as it is now)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import git_discovery, links, worktree
from agent_cli.core import ExternalFailure

# `_owned` compares the owner with `os.getuid`, which Windows lacks, so there every answer comes
# from git and these tests' paths through `main_checkout` are what still holds.
no_fast_path = pytest.mark.skipif(
    sys.platform == "win32", reason="no os.getuid on Windows: common_dir always defers to git"
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "f.txt").write_text("x\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _rewrite(path: Path, text: str) -> None:
    """Replace a file's text in place: git for Windows hides a `.git` gitfile, and Windows refuses
    to open a hidden file for a truncating write."""
    with path.open("r+", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.truncate()


def _by_git(path: Path) -> Path:
    common = Path(_git(path, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else (path / common)).resolve().parent


@pytest.fixture
def no_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite fences git discovery at its temp root; these tests set their own ceilings."""
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)


def _nested(tmp_path: Path) -> tuple[Path, list[Path]]:
    repo = _repo(tmp_path / "repo")
    inside = repo / "src" / "inside"
    inside.mkdir(parents=True)
    return repo, [repo, inside]


def _linked(tmp_path: Path) -> tuple[Path, list[Path]]:
    repo = _repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    nested = linked / "deep"
    nested.mkdir()
    return repo, [linked, nested]


def test_the_main_checkout_and_a_nested_path_agree_with_git(tmp_path: Path, no_ceiling: None):
    repo, paths = _nested(tmp_path)
    for path in paths:
        assert worktree.main_checkout(path) == _by_git(path) == repo.resolve()


@no_fast_path
def test_the_main_checkout_and_a_nested_path_are_read_without_git(tmp_path: Path, no_ceiling: None):
    _, paths = _nested(tmp_path)
    for path in paths:
        assert git_discovery.common_dir(path) is not None


def test_a_linked_worktree_resolves_through_its_gitfile(tmp_path: Path, no_ceiling: None):
    repo, paths = _linked(tmp_path)
    for path in paths:
        assert worktree.main_checkout(path) == _by_git(path) == repo.resolve()


@no_fast_path
def test_a_linked_worktree_is_read_without_git(tmp_path: Path, no_ceiling: None):
    _, paths = _linked(tmp_path)
    for path in paths:
        assert git_discovery.common_dir(path) is not None, "read from the gitfile and commondir"


def test_outside_any_repository_it_raises_as_git_does(
    tmp_path: Path, no_ceiling: None, monkeypatch: pytest.MonkeyPatch
):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    with pytest.raises(ExternalFailure, match="not a git repository"):
        worktree.main_checkout(plain)
    with pytest.raises(subprocess.CalledProcessError):
        _git(plain, "rev-parse", "--git-common-dir")


def test_a_path_that_becomes_a_repository_is_seen_at_once(
    tmp_path: Path, no_ceiling: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    root = tmp_path / "later"
    root.mkdir()
    with pytest.raises(ExternalFailure):
        worktree.main_checkout(root)
    _git(root, "init", "-q", "-b", "main")
    assert worktree.main_checkout(root) == root.resolve()


def test_a_recreated_worktree_path_reports_its_new_owner(tmp_path: Path, no_ceiling: None):
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second")
    path = tmp_path / "wt"
    _git(first, "worktree", "add", "-q", "-b", "a", str(path))
    assert worktree.main_checkout(path) == first.resolve()
    _git(first, "worktree", "remove", "--force", str(path))
    _git(second, "worktree", "add", "-q", "-b", "b", str(path))
    assert worktree.main_checkout(path) == _by_git(path) == second.resolve()


def test_a_ceiling_stops_the_walk(tmp_path: Path, no_ceiling: None, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path / "repo")
    below = repo / "a" / "b"
    below.mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str((repo / "a").resolve()))
    with pytest.raises(ExternalFailure):
        worktree.main_checkout(below)
    with pytest.raises(subprocess.CalledProcessError):
        _git(below, "rev-parse", "--git-common-dir")


@pytest.mark.parametrize("override", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_an_environment_override_defers_to_git(
    tmp_path: Path, no_ceiling: None, monkeypatch: pytest.MonkeyPatch, override: str
):
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv(override, str(repo / ".git"))
    assert git_discovery.common_dir(repo) is None
    monkeypatch.setenv(override, "")  # git rejects an empty override; only git reports it
    assert git_discovery.common_dir(repo) is None


def test_a_bare_repository_and_an_invalid_dotgit_defer_to_git(tmp_path: Path, no_ceiling: None):
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    assert git_discovery.common_dir(bare) is None
    fake = tmp_path / "fake"
    (fake / ".git").mkdir(parents=True)  # an empty .git directory is not a repository
    assert git_discovery.common_dir(fake) is None


def test_the_suite_s_harness_root_layouts_agree_with_git(tmp_path: Path, no_ceiling: None):
    repo = _repo(tmp_path / "harness")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--no-local", str(repo), str(clone))
    shutil.rmtree(clone / ".git" / "hooks", ignore_errors=True)
    for path in (repo, clone):
        assert worktree.main_checkout(path) == _by_git(path)


# Codex's phase 0-2 review: each layout below made git and the earlier reading disagree.


def test_a_copied_linked_worktree_gitdir_defers_to_git(tmp_path: Path, no_ceiling: None):
    repo = _repo(tmp_path / "repo")
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    copy = tmp_path / "copy"
    copy.mkdir()
    gitdir = Path(_git(tmp_path / "wt", "rev-parse", "--absolute-git-dir"))
    shutil.copytree(gitdir, copy / ".git")
    (copy / ".git" / "commondir").write_text(str(repo / ".git") + "\n")
    assert git_discovery.common_dir(copy) is None
    assert worktree.main_checkout(copy) == _by_git(copy) == repo.resolve()


def test_a_malformed_head_or_gitfile_defers_to_git(tmp_path: Path, no_ceiling: None):
    repo = _repo(tmp_path / "repo")
    (repo / ".git" / "HEAD").write_text("not a ref\n")
    assert git_discovery.common_dir(repo) is None
    other = _repo(tmp_path / "other")
    _git(other, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    gitfile = tmp_path / "wt" / ".git"
    _rewrite(gitfile, gitfile.read_text().replace("gitdir: ", "gitdir:"))
    assert git_discovery.common_dir(tmp_path / "wt") is None


def test_a_foreign_owner_on_the_worktree_defers_to_git(
    tmp_path: Path, no_ceiling: None, monkeypatch: pytest.MonkeyPatch
):
    repo = _repo(tmp_path / "repo")
    real = git_discovery._owned  # pyright: ignore[reportPrivateUsage]

    def owned(path: Path) -> bool:
        return path.resolve() != repo.resolve() and real(path)

    monkeypatch.setattr(git_discovery, "_owned", owned)
    assert git_discovery.common_dir(repo) is None


def _through_a_link(tmp_path: Path) -> tuple[Path, Path]:
    """A worktree whose gitfile names its gitdir through a directory link (a junction on Windows
    without symlink rights)."""
    repo = _repo(tmp_path / "real" / "repo")
    links.make(tmp_path / "link", tmp_path / "real", is_dir=True)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    gitfile = wt / ".git"
    target = Path(gitfile.read_text().removeprefix("gitdir: ").strip()).as_posix()
    linked = target.replace((tmp_path / "real").resolve().as_posix(), (tmp_path / "link").as_posix())
    assert linked != target
    _rewrite(gitfile, "gitdir: " + linked + "\n")
    return repo, wt


def test_an_absolute_gitdir_through_a_symlink_is_resolved(tmp_path: Path, no_ceiling: None):
    _, wt = _through_a_link(tmp_path)
    assert worktree.main_checkout(wt) == _by_git(wt)


@no_fast_path
def test_an_absolute_gitdir_through_a_symlink_is_resolved_without_git(tmp_path: Path, no_ceiling: None):
    repo, wt = _through_a_link(tmp_path)
    assert git_discovery.common_dir(wt) == (repo / ".git").resolve()


def test_an_empty_ceiling_entry_defers_to_git(
    tmp_path: Path, no_ceiling: None, monkeypatch: pytest.MonkeyPatch
):
    repo = _repo(tmp_path / "repo")
    (repo / "src").mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", f":{repo / 'src'}")
    assert git_discovery.common_dir(repo / "src") is None
    assert worktree.main_checkout(repo / "src") == _by_git(repo / "src") == repo.resolve()
