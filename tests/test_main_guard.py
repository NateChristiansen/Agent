"""Main moves only through `agent land` (hooks/main_guard.py, inside commit-guard): the spellings
that move main are refused on both clients' payloads, from the main checkout, a linked worktree
or `-C`; branch work is not."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.hooks.commit_guard import commit_guard
from land_helpers import git as _git

QUEUE = "harness/lessons/entry.md"


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "harness"
    (root / "harness" / "lessons").mkdir(parents=True)
    (root / QUEUE).write_text("# queue\n")
    (root / "b.txt").write_text("b\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "worktree", "add", "-q", "-b", "side", str(tmp_path / "wt"))
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


# The guard only reads the repo, so the parametrized tables below share one repo per module.
@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_repo(tmp_path_factory.mktemp("plain"))


@pytest.fixture(scope="module")
def staged(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    repo = _make_repo(tmp_path_factory.mktemp("staged"))
    wt = repo.parent / "wt"
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "side work")
    head = _git(wt, "rev-parse", "HEAD")
    base = _git(repo, "rev-parse", "HEAD~0")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    return repo, head, base


@pytest.fixture(scope="module")
def behind(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = _make_repo(tmp_path_factory.mktemp("behind"))
    _behind_origin(repo, repo.parent)
    return repo


def _decide(root: Path, command: str, cwd: Path, codex: bool) -> str:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    if codex:
        payload.update(hook_event_name="PreToolUse", turn_id="t1", model="gpt-5")
    else:
        payload.update(session_id="", transcript_path="/dev/null")
    out = commit_guard(root, payload)
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else ""


DENIED = [
    ("git update-ref HEAD {head}", "main"),
    ("git update-ref refs/heads/main {head}", "wt"),
    ("git -C {main} update-ref main {head}", "elsewhere"),
    ("git update-ref --stdin", "main"),
    ("git update-ref -d refs/heads/main", "wt"),
    ("git switch -C main {head}", "wt"),
    ("git checkout -B main {head}", "wt"),
    ("git branch -M side main", "wt"),
    ("git branch -f main {head}", "wt"),
    ("git fetch . side:main", "wt"),
    ("git fetch origin +side:refs/heads/main", "main"),
    ("git push . side:main", "wt"),
    ("git -c alias.zz=update-ref zz refs/heads/main {head}", "wt"),
    ("git commit -m x", "main"),
    ("git commit --amend -m x", "main"),
    ("git -C {main} commit -m x", "elsewhere"),
    ("git reset --hard {base}", "main"),
    ("git merge side", "main"),
    ("git cherry-pick side", "main"),
    ("git pull", "main"),
    ("git filter-branch --force HEAD", "wt"),
    # Review cases: a trailing `--`, attached and `=` option values, renaming main itself,
    # an option value before update-ref's ref, and an option before push's `.`
    ("git reset --hard {base} --", "main"),
    ("git switch -Cmain {head}", "wt"),
    ("git switch --force-create=main {head}", "wt"),
    ("git checkout -Bmain {head}", "wt"),
    ("git worktree add -Bmain ../wt-new {head}", "main"),
    ("git branch -M renamed-main", "main"),
    ("git update-ref -m msg refs/heads/main {head}", "wt"),
    ("git push --push-option x . side:main", "wt"),
    ("git branch -m main side-two", "wt"),
    ("git branch -C side main", "wt"),
    ("git symbolic-ref refs/heads/main refs/heads/side", "wt"),
    # A second name for main, through which a fetch or update-ref moves it
    ("git symbolic-ref refs/agent/sync/main refs/heads/main", "wt"),
    ("git symbolic-ref -m why refs/heads/alias main", "main"),
    ("git fetch . '+refs/heads/*:refs/heads/*'", "wt"),
]


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("template,where", DENIED)
def test_spellings_that_move_main_are_refused(
    staged: tuple[Path, str, str], template: str, where: str, codex: bool
):
    repo, head, base = staged
    command = template.format(head=head, main=repo, base=base)
    cwd = {"main": repo, "wt": repo.parent / "wt", "elsewhere": repo.parent}[where]
    assert _decide(repo, command, cwd, codex) == "deny", command


def test_a_configured_alias_is_expanded(repo: Path, tmp_path: Path):
    _git(repo, "config", "alias.ur", "update-ref")
    head = _git(repo, "rev-parse", "HEAD")
    assert _decide(repo, f"git ur refs/heads/main {head}", tmp_path / "wt", False) == "deny"
    _git(repo, "config", "alias.mv", "!git update-ref refs/heads/main")
    assert _decide(repo, f"git mv {head}", tmp_path / "wt", False) == "deny"


ALLOWED = [
    ("git commit -m side", "wt"),
    ("git update-ref refs/heads/side {head}", "wt"),
    ("git switch main", "wt"),
    ("git status", "main"),
    ("git log --oneline", "main"),
    ("git reset", "main"),
    ("git reset HEAD b.txt", "main"),
    ("git merge --abort", "main"),
    ("git branch -f side {head}", "main"),
    ("git fetch origin main:refs/remotes/origin/main", "main"),
    ("git reset {head} -- b.txt", "main"),
    ("git branch -c copy-of-main", "main"),
    ("git branch -M renamed-side", "wt"),
    # `branch -f` moves its first name only; reads of a symbolic ref; a glob into remote-tracking refs
    ("git branch -f side main", "main"),
    ("git branch -c main main-copy", "wt"),
    ("git symbolic-ref -q HEAD", "main"),
    ("git fetch origin '+refs/heads/*:refs/remotes/origin/*'", "main"),
]


@pytest.mark.parametrize("template,where", ALLOWED)
def test_branch_work_and_reads_pass(plain: Path, template: str, where: str):
    repo = plain
    head = _git(repo, "rev-parse", "HEAD")
    cwd = {"main": repo, "wt": repo.parent / "wt"}[where]
    assert _decide(repo, template.format(head=head), cwd, False) == "", template


def test_a_commit_of_the_old_lessons_queue_alone_is_refused_on_main(repo: Path):
    (repo / QUEUE).write_text("# queue\n- a lesson\n")
    assert _decide(repo, "git commit -am 'lessons: one'", repo, False) == "deny"
    _git(repo, "add", QUEUE)
    assert _decide(repo, "git commit -m 'lessons: one'", repo, True) == "deny"


def test_a_product_repo_is_not_the_harness(repo: Path, tmp_path: Path):
    product = tmp_path / "product"
    product.mkdir()
    _git(product, "init", "-q", "-b", "main")
    _git(product, "commit", "-q", "--allow-empty", "-m", "base")
    assert _decide(repo, "git commit --allow-empty -m x", product, False) == ""
    assert _decide(repo, "git reset --hard HEAD", product, False) == ""


def _behind_origin(repo: Path, tmp_path: Path, *, upstream: bool = True) -> Path:
    """Main pushed to a bare origin, then another machine lands and pushes one more commit; the
    harness has fetched it, so main is one behind `origin/main` (D11(b): Windows lands via the Mac)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", *(["-u"] if upstream else []), "origin", "main")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    _git(other, "commit", "-q", "--allow-empty", "-m", "landed on the other machine")
    _git(other, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    return other


#: Every spelling that would fast-forward main to the pushed tip, all refused: config, refspecs,
#: tags and remotes are mutable, so the hook cannot tell the pushed tip from another commit (Codex
#: P5 review, round 1). `repos_sync` harness=true is the one path (test_main_sync.py).
PULLS_AND_MERGES = [
    "git pull",
    "git pull --ff-only",
    "git pull -q --ff-only origin",
    "git pull --ff-only origin main",
    "git pull --ff-only --no-rebase origin refs/heads/main",
    "git pull --ff-only --rebase",
    "git pull --ff-only origin main:main",
    "git -c remote.origin.url=/tmp/x pull --ff-only",
    "git merge origin/main",
    "git merge --ff-only origin/main",
    "git merge --ff-only --quiet refs/remotes/origin/main",
    "git merge --ff-only origin/main~0",
]


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("command", PULLS_AND_MERGES)
def test_every_pull_and_merge_on_main_is_refused(behind: Path, command: str, codex: bool):
    assert _decide(behind, command, behind, codex) == "deny", command


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_aliases_of_main_through_head_or_chains_are_refused(repo: Path, tmp_path: Path, codex: bool):
    """With HEAD on main, a symref to HEAD is a second name for
    main, and `update-ref` through it moved main. Targets and write destinations are resolved
    through every symbolic ref before deciding."""
    _git(tmp_path / "wt", "commit", "-q", "--allow-empty", "-m", "uncertified side")
    side = _git(tmp_path / "wt", "rev-parse", "HEAD")
    alias = "refs/agent/indirect-main"
    assert _decide(repo, f"git symbolic-ref {alias} HEAD", repo, codex) == "deny"
    _git(repo, "symbolic-ref", alias, "HEAD")  # made outside the guard
    assert _decide(repo, f"git update-ref {alias} {side}", repo, codex) == "deny"
    assert _decide(repo, f"git update-ref --no-deref -d {alias}", repo, codex) == ""  # removes the alias
    assert _decide(repo, f"git symbolic-ref refs/agent/second {alias}", repo, codex) == "deny"
    _git(repo, "symbolic-ref", "refs/heads/alias-branch", "refs/heads/main")
    assert _decide(repo, f"git branch -f alias-branch {side}", tmp_path / "wt", codex) == "deny"
    assert _decide(repo, "git push . side:refs/heads/alias-branch", tmp_path / "wt", codex) == "deny"
    assert _decide(repo, f"git update-ref refs/agent/plain {side}", repo, codex) == ""


@pytest.mark.parametrize(
    "command",
    ["git worktree add --force ../extra main", "git worktree add -f ../extra main"],
)
def test_a_second_checkout_of_main_is_refused(plain: Path, command: str):
    """Round 3, probe 3: `worktree add --force` checked main out twice; a sync then left one stale."""
    wt = plain.parent / "wt"
    assert _decide(plain, command, wt, False) == "deny"
    assert _decide(plain, "git checkout --ignore-other-worktrees main", wt, True) == "deny"
    assert _decide(plain, "git worktree add ../extra side", plain, False) == ""


def test_a_tag_named_origin_main_changes_nothing(repo: Path, tmp_path: Path):
    """Codex's probe: a local tag `origin/main` on another commit. The hook refuses the merge
    whatever the operand resolves to, and the sync action moves by sha, never by name."""
    _behind_origin(repo, tmp_path)
    _git(tmp_path / "wt", "commit", "-q", "--allow-empty", "-m", "alternative")
    _git(repo, "tag", "origin/main", _git(tmp_path / "wt", "rev-parse", "HEAD"))
    assert _decide(repo, "git merge --ff-only origin/main", repo, False) == "deny"


def test_the_refusal_names_the_sync_action(behind: Path):
    out = commit_guard(
        behind,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git pull --ff-only"},
            "cwd": str(behind),
            "session_id": "",
        },
    )
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "repos_sync" in decision["permissionDecisionReason"]
