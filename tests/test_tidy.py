"""Execute cleanup against disposable git trees, including squash landings."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import seeded
from agent_cli import core, features, land_journal, leases, ops, repos, sessions, tidy, vault
from agent_cli.cli import parse
from agent_cli.doctor.checks_repos import closed_leftovers
from agent_cli.mcp.specs_harness import harness
from agent_cli.mcp.tools import Ctx
from conftest import git


@pytest.fixture(scope="module")
def _repo_template(
    tmp_path_factory: pytest.TempPathFactory, _root_template: Path, _notes_template: Path
) -> Path:
    """The root as a git repo with feature 1 claimed, built once per module and copied per test."""
    base = tmp_path_factory.mktemp("tidy-template")
    root = seeded.base_root(base, _root_template, _notes_template)
    with seeded.seeding(root):
        git(["init", "-b", "main"], root)
        git(["config", "user.name", "Fixture"], root)
        git(["config", "user.email", "fixture@example.invalid"], root)
        (root / "tracked.txt").write_text("base\n")
        (root / ".gitignore").write_text(".agent/\nfeatures/\nagent.yaml\n")
        git(["add", "tracked.txt", ".gitignore"], root)
        git(["commit", "-m", "base"], root)
        features.new(root, "cleanup", "Cleanup")
    return base


@pytest.fixture
def repo(tmp_root: Path, _repo_template: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    seeded.install(_repo_template, tmp_root.parent)
    monkeypatch.chdir(tmp_root)
    monkeypatch.setattr(tidy, "process_cwds", lambda: [])
    return tmp_root


def tree(root: Path, name: str = "001-cleanup") -> Path:
    path = root.parent / name
    git(["worktree", "add", "-b", f"agent/{name}", str(path), "main"], root)
    return path


def close(root: Path) -> None:
    features.close(root, features.resolve(root, "1"))


@pytest.mark.parametrize("hold", ["session", "cwd", "lease", "dirty", "unresolved"])
def test_feature_close_preserves_each_d7_hold(repo: Path, monkeypatch, hold: str):
    wt = tree(repo)
    child = wt / "nested"
    child.mkdir()
    if hold == "session":
        sessions.set_record(repo, "holder", cwd=str(child), pid=os.getpid())
    elif hold == "cwd":
        monkeypatch.setattr(tidy, "process_cwds", lambda: [(123, str(child))])
    elif hold == "lease":
        sessions.set_record(repo, "holder", cwd=str(repo), pid=os.getpid())
        leases.take_marked(
            repo,
            "worktree:agent/001-cleanup",
            session_id="holder",
            pid=os.getpid(),
            feature={"id": 1},
            worktree_key=str(wt),
        )
    elif hold == "dirty":
        (wt / "tracked.txt").write_text("keep\n")
    else:
        (wt / "tracked.txt").write_text("branch work\n")
        git(["add", "tracked.txt"], wt)
        git(["commit", "-m", "unresolved work"], wt)
    result = ops.feature_close(repo, feature="1")
    assert wt.exists()
    assert result.data["worktrees_kept"]
    assert not result.data["worktrees_removed"]
    assert "agent/001-cleanup" in git(["branch", "--list"], repo)
    if hold in ("dirty", "unresolved"):
        assert "kept: unresolved work" in result.text
        assert not result.data["worktrees_kept_historical"]
    else:
        assert "kept: historical checkout, safe once branch resolved" in result.text
        assert not result.data["worktrees_kept_unresolved"]


def test_feature_close_ignores_the_callers_own_leases(repo: Path):
    wt = tree(repo)
    sessions.set_record(repo, "caller", cwd=str(repo), pid=os.getpid())
    for area in ("vault:src", "worktree:agent/001-cleanup"):
        leases.take(
            repo,
            area,
            session_id="caller",
            pid=os.getpid(),
            feature={"id": 1},
            marks={"worktree_key": str(wt)} if area.startswith("worktree:") else None,
        )
    result = ops.feature_close(repo, feature="1", session_id="caller")
    assert result.data["worktrees_removed"] == [str(wt)]
    assert not wt.exists()


def test_another_live_sessions_vault_lease_does_not_hold_the_feature_tree(repo: Path):
    wt = tree(repo)
    sessions.set_record(repo, "holder", cwd=str(repo), pid=os.getpid())
    leases.take(repo, "vault:src", session_id="holder", pid=os.getpid(), feature={"id": 1})
    result = ops.feature_close(repo, feature="1")
    assert result.data["worktrees_removed"] == [str(wt)]
    assert not wt.exists()


@pytest.mark.parametrize("operation", ["close", "tidy"])
def test_a_dead_holders_tree_lease_does_not_block_close_or_tidy(repo: Path, monkeypatch, operation):
    wt = tree(repo)
    sessions.set_record(repo, "holder", cwd=str(wt), pid=12345)
    leases.take_marked(
        repo,
        "worktree:agent/001-cleanup",
        session_id="holder",
        pid=12345,
        feature={"id": 1},
        worktree_key=str(wt),
    )
    monkeypatch.setattr(tidy.core, "is_alive", lambda pid: False)
    if operation == "close":
        result = ops.feature_close(repo, feature="1")
        assert result.data["worktrees_removed"] == [str(wt)]
    else:
        close(repo)
        assert tidy.inventory(repo)["trees"][0]["holds"] == []
        assert tidy.run(repo, apply=True)["removed"] == [str(wt)]
    assert not wt.exists()


def test_unkeyed_worktree_leases_match_the_repository_as_well_as_the_name(repo: Path):
    wt = tree(repo)
    leases.take(repo, f"worktree:other-repo/{wt.name}", session_id="holder", pid=os.getpid())
    assert not tidy.active_holds(repo, wt, 1)
    leases.take(repo, f"worktree:harness/{wt.name}", session_id="holder", pid=os.getpid())
    assert tidy.active_holds(repo, wt, 1)


def test_a_live_lease_below_the_tree_blocks_cleanup(repo: Path):
    wt = tree(repo)
    sessions.set_record(repo, "holder", cwd=str(repo), pid=os.getpid())
    leases.take_marked(
        repo,
        "vault:src",
        session_id="holder",
        pid=os.getpid(),
        worktree_key=str(wt / "nested"),
    )
    assert tidy.active_holds(repo, wt, 1) == ["lease vault:src"]


def test_a_generated_name_prefix_is_real_untracked_work(repo: Path):
    wt = tree(repo)
    (wt / ".btconfig").mkdir()
    (wt / ".btconfig" / "real.py").write_text("real work\n")
    assert not tidy.generated(".btconfig/real.py")
    assert tidy.changes(wt) == [".btconfig/real.py"]
    result = ops.feature_close(repo, feature="1")
    assert wt.exists() and result.data["worktrees_kept"]
    assert not result.data["worktrees_removed"]


def squash(root: Path, wt: Path) -> str:
    (wt / "tracked.txt").write_text("landed work\n")
    git(["add", "tracked.txt"], wt)
    git(["commit", "-m", "work"], wt)
    tip = git(["rev-parse", "HEAD"], wt).strip()
    git(["merge", "--squash", "agent/001-cleanup"], root)
    git(["commit", "-m", "squashed work"], root)
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", tip, "main"],
            cwd=root,
            check=False,
        ).returncode
        == 1
    )
    land_journal.write(
        root,
        {
            "id": "20261014T120000-test",
            "state": "done",
            "branch": "agent/001-cleanup",
            "tip": tip,
            "cand": git(["rev-parse", "main"], root).strip(),
        },
    )
    # Main moved on; equal current trees alone no longer proves the branch landed.
    (root / "later.txt").write_text("later\n")
    git(["add", "later.txt"], root)
    git(["commit", "-m", "later"], root)
    return tip


def test_close_removes_squash_landed_generated_only_tree_and_keeps_branch(repo: Path):
    wt = tree(repo)
    squash(repo, wt)
    for directory in (".bt1", ".pytest-tmp", "pytest-temproot"):
        (wt / directory).mkdir()
        (wt / directory / "output.txt").write_text("generated\n")
    result = ops.feature_close(repo, feature="1")
    assert result.data["worktrees_removed"] == [str(wt)]
    assert not wt.exists()
    assert "agent/001-cleanup" in git(["branch", "--list"], repo)


def test_tidy_dry_run_is_read_only_and_apply_only_removes_resolved(repo: Path):
    good = tree(repo)
    squash(repo, good)
    bad = tree(repo, "001-unresolved")
    (bad / "tracked.txt").write_text("unresolved\n")
    git(["add", "tracked.txt"], bad)
    git(["commit", "-m", "unresolved"], bad)
    close(repo)
    before = git(["worktree", "list", "--porcelain"], repo)
    state = sorted((repo / ".agent").rglob("*"))
    result = tidy.operation(repo)
    assert result.data["dry_run"] is True
    assert before == git(["worktree", "list", "--porcelain"], repo)
    assert state == sorted((repo / ".agent").rglob("*"))
    checks = closed_leftovers(repo)
    assert len([c for c in checks if c["check"] == "finished worktree"]) == 2
    result = tidy.run(repo, apply=True)
    assert result["removed"] == [str(good)]
    assert result["branches_deleted"] == ["agent/001-cleanup"]
    assert bad.exists()
    assert "agent/001-unresolved" in git(["branch", "--list"], repo)


def test_a_post_landing_commit_is_unresolved(repo: Path):
    wt = tree(repo)
    squash(repo, wt)
    (wt / "extra.txt").write_text("new work\n")
    git(["add", "extra.txt"], wt)
    git(["commit", "-m", "new work"], wt)
    close(repo)
    assert tidy.inventory(repo)["trees"][0]["holds"] == [tidy.UNRESOLVED_BRANCH]
    assert "`dispatch` action `cleanup` with force set" in tidy.UNRESOLVED_BRANCH


@pytest.mark.parametrize("directory", [".bt1", ".ruff_cache", ".mypy_cache", ".pyright"])
def test_tracked_generated_named_changes_are_preserved(repo: Path, directory: str):
    wt = tree(repo)
    (wt / directory).mkdir()
    name = f"{directory}/tracked.py"
    (wt / name).write_text("tracked\n")
    git(["add", name], wt)
    assert tidy.changes(wt) == [name]


def test_process_inventory_observes_a_real_child_cwd(repo: Path, monkeypatch):
    from agent_cli import tidy as module

    # Restore the real read-only inventory; the reaper fixture stays disabled.
    original = process_inventory
    monkeypatch.setattr(module, "process_cwds", original)
    wt = tree(repo)
    # A Windows venv python.exe is a launcher that runs the base interpreter as a second process.
    python = getattr(sys, "_base_executable", sys.executable) if sys.platform == "win32" else sys.executable
    child = subprocess.Popen([python, "-c", "import sys; sys.stdin.read()"], cwd=wt, stdin=subprocess.PIPE)
    try:
        close(repo)
        rows = tidy.inventory(repo)["trees"]
        assert f"process cwd {child.pid}" in rows[0]["holds"]
        assert not tidy.run(repo, apply=True)["removed"]
        assert wt.exists()
    finally:
        child.communicate(timeout=10)


process_inventory = tidy.process_cwds


def test_tidy_parser_defaults_to_read_only():
    assert parse(["tidy"]).apply is False
    assert parse(["tidy", "--dry-run"]).apply is False
    assert parse(["tidy", "--apply"]).apply is True


def test_an_unavailable_process_inventory_keeps_and_reports_the_tree(repo: Path, monkeypatch):
    from agent_cli.core import AgentError

    wt = tree(repo)
    close(repo)

    def failed():
        raise AgentError("inventory unavailable")

    monkeypatch.setattr(tidy, "process_cwds", failed)
    listing = tidy.run(repo, apply=True)
    assert "process cwd inspection failed" in listing["trees"][0]["holds"][0]
    assert wt.exists() and listing["removed"] == []
    assert closed_leftovers(repo)[0]["level"] == "warn"


def test_apply_rechecks_a_process_that_entered_after_inventory(repo: Path, monkeypatch):
    wt = tree(repo)
    close(repo)
    reads = iter([[], [(123, str(wt))]])
    monkeypatch.setattr(tidy, "process_cwds", lambda: next(reads))
    listing = tidy.run(repo, apply=True)
    assert listing["trees"][0]["holds"] == ["process cwd 123"]
    assert wt.exists() and listing["removed"] == []


def test_pull_warn_skips_a_harness_read_from_a_linked_manager_tree(repo: Path, monkeypatch):
    from agent_cli import repos
    from agent_cli.hooks.repo_guards import pull_warn

    wt = tree(repo)
    registry = repos.Registry(1, [repos.Repo("Harness", "agent", str(repo), "unused")])
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    assert (
        pull_warn(
            wt,
            {
                "session_id": "reader",
                "tool_name": "Read",
                "tool_input": {"file_path": str(repo / "tracked.txt")},
            },
        )
        == ""
    )


def test_view_writers_share_the_cleanup_registry():
    from agent_cli import status

    assert set(core.GENERATED_VIEWS.values()) == {
        core.GENERATED_VIEWS["status"],  # retired output stays disposable (Audit 10 K1)
        status.ADR_INDEX,
        *vault.GENERATED,
    }


@pytest.mark.parametrize("tracked", [False, True])
def test_harness_generated_only_tree_is_removable(repo: Path, tracked: bool):
    wt = tree(repo)
    for path in core.GENERATED_VIEWS.values():
        target = wt / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old generated view\n")
    if tracked:
        git(["add", "-f", *map(str, core.GENERATED_VIEWS.values())], wt)
        git(["commit", "-m", "views"], wt)
        git(["merge", "--ff-only", "agent/001-cleanup"], repo)
        for path in core.GENERATED_VIEWS.values():
            (wt / path).write_text("regenerated view\n")
    for path in core.REGENERABLE_STATE:
        target = wt / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("runtime output\n")
    close(repo)
    assert tidy.inventory(repo)["trees"][0]["holds"] == []
    assert tidy.run(repo, apply=True)["removed"] == [str(wt)]


def test_ignored_durable_local_state_holds_the_tree(repo: Path):
    wt = tree(repo)
    paths = ["dispatches/work.json", "leases/work.json", "peers/inbox.json", "scratch/draft.md"]
    for path in paths:
        target = wt / ".agent" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("durable work\n")
    close(repo)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == []
    for path in paths:
        assert (wt / ".agent" / path).read_text() == "durable work\n"
        assert f".agent/{path}" in result["trees"][0]["holds"][0]


def test_real_adr_and_unknown_base_edits_still_hold_a_harness_tree(repo: Path):
    wt = tree(repo)
    target = wt / "adrs/0001-real.md"
    target.parent.mkdir()
    target.write_text("an ADR edit\n")
    (target.parent / "README.md").write_text("generated\n")
    (wt / "bases").mkdir()
    base = wt / "bases/custom.base"
    base.write_text("owner work\n")
    close(repo)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == []
    assert result["trees"][0]["holds"] == ["uncommitted changes: adrs/0001-real.md, bases/custom.base"]
    assert target.exists()
    assert base.read_text() == "owner work\n"


def test_shared_agent_link_is_unlinked_without_touching_durable_target(repo: Path):
    from agent_cli import links

    wt = tree(repo)
    target = repo / ".agent/dispatches/shared.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text("shared record\n")
    links.make(wt / ".agent", repo / ".agent", is_dir=True)
    close(repo)
    assert tidy.inventory(repo)["trees"][0]["holds"] == []
    assert tidy.run(repo, apply=True)["removed"] == [str(wt)]
    assert target.read_text() == "shared record\n"


@pytest.mark.parametrize("operation", ["close", "tidy"])
def test_ignored_private_links_do_not_hold_or_remove_their_targets(repo: Path, operation: str):
    from agent_cli import links

    wt = tree(repo)
    private = repo.parent / "private"
    private.mkdir()
    draft = private / "draft.md"
    draft.write_text("private draft\n")
    config = private / "config.yaml"
    config.write_text("private config\n")
    with (repo / ".git/info/exclude").open("a") as stream:
        stream.write("\nfeatures\n")
    links.make(wt / "features", private, is_dir=True)
    links.make(wt / "agent.yaml", config, is_dir=False)
    # A hard link fallback is a regular file and remains conservatively held.
    if not links.is_link(wt / "agent.yaml"):
        pytest.skip("file symlinks unavailable")
    assert tidy.ignored_files(wt) == []
    if operation == "close":
        assert ops.feature_close(repo, feature="1").data["worktrees_removed"] == [str(wt)]
    else:
        close(repo)
        assert tidy.run(repo, apply=True)["removed"] == [str(wt)]
    assert not wt.exists()
    assert draft.read_text() == "private draft\n"
    assert config.read_text() == "private config\n"


def test_product_worktree_generated_names_still_hold_edits(repo: Path, monkeypatch):
    product = repo.parent / "product"
    product.mkdir()
    git(["init", "-b", "main"], product)
    git(["config", "user.name", "Fixture"], product)
    git(["config", "user.email", "fixture@example.invalid"], product)
    filenames = ["STATUS.md", "bases/x.base"]
    for filename in filenames:
        target = product / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("product source\n")
    git(["add", "."], product)
    git(["commit", "-m", "product"], product)
    registry = repos.Registry(1, [repos.Repo("Product", "product", str(product), "unused")])
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    wt = product.parent / "product-worktree"
    git(["worktree", "add", "-b", "agent/001-product", str(wt)], product)
    for filename in filenames:
        (wt / filename).write_text("real product edit\n")
    close(repo)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == []
    for filename in filenames:
        assert filename in result["trees"][0]["holds"][0]
        assert (wt / filename).read_text() == "real product edit\n"


def test_two_squashed_commits_resolve_after_unrelated_main_change_without_records(repo: Path):
    wt = tree(repo)
    for content in ("first\n", "second\n"):
        (wt / "tracked.txt").write_text(content)
        git(["commit", "-am", content.strip()], wt)
    git(["merge", "--squash", "agent/001-cleanup"], repo)
    git(["commit", "-m", "squashed"], repo)
    (repo / "unrelated.txt").write_text("unrelated\n")
    git(["add", "unrelated.txt"], repo)
    git(["commit", "-m", "unrelated"], repo)
    close(repo)
    assert tidy.inventory(repo)["trees"][0]["holds"] == []
    assert tidy.run(repo, apply=True)["removed"] == [str(wt)]


def test_unlanded_mode_change_stays_held(repo: Path):
    wt = tree(repo)
    blob = git(["rev-parse", "HEAD:tracked.txt"], wt).strip()
    git(["update-index", "--cacheinfo", f"100755,{blob},tracked.txt"], wt)
    git(["commit", "-m", "executable"], wt)
    assert not tidy.resolved(repo, repo, "agent/001-cleanup")
    close(repo)
    assert tidy.run(repo, apply=True)["removed"] == []
    assert wt.exists()


def test_rename_source_deletion_is_not_satisfied_by_a_copy_on_main(repo: Path):
    wt = tree(repo)
    git(["mv", "tracked.txt", "destination.txt"], wt)
    git(["commit", "-m", "rename"], wt)
    (repo / "destination.txt").write_bytes((repo / "tracked.txt").read_bytes())
    git(["add", "destination.txt"], repo)
    git(["commit", "-m", "copy"], repo)
    close(repo)
    assert tidy.inventory(repo)["trees"][0]["holds"] == [tidy.UNRESOLVED_BRANCH]
    assert tidy.run(repo, apply=True)["removed"] == []
    assert wt.exists()


def test_landing_evidence_still_resolves_after_main_changes_the_landed_path(repo: Path):
    wt = tree(repo)
    tip = squash(repo, wt)
    (repo / "tracked.txt").write_text("a subsequent edit\n")
    git(["commit", "-am", "later main edit"], repo)
    journal = land_journal.every(repo)[0]
    journal["members"] = [{"branch": "agent/001-cleanup", "tip": tip}]
    journal.pop("branch")
    journal.pop("tip")
    land_journal.write(repo, journal)
    assert tidy.resolved(repo, repo, "agent/001-cleanup")


def test_staged_rename_into_a_generated_path_preserves_real_source_deletion(repo: Path):
    wt = tree(repo)
    git(["mv", "tracked.txt", "STATUS.md"], wt)
    close(repo)
    result = tidy.run(repo, apply=True)
    assert result["removed"] == []
    assert result["trees"][0]["holds"] == ["uncommitted changes: tracked.txt"]


def test_apply_rechecks_real_edit_made_after_generated_only_inventory(repo: Path, monkeypatch):
    wt = tree(repo)
    (wt / "STATUS.md").write_text("generated\n")
    close(repo)
    inventory = tidy.inventory

    def inspect(*args, **kwargs):
        result = inventory(*args, **kwargs)
        assert result["trees"][0]["holds"] == []
        (wt / "tracked.txt").write_text("edit after inventory\n")
        return result

    monkeypatch.setattr(tidy, "inventory", inspect)
    assert tidy.run(repo, apply=True)["removed"] == []
    assert (wt / "tracked.txt").read_text() == "edit after inventory\n"


def test_mcp_tidy_finishes_fast_or_returns_a_detached_log(repo: Path, monkeypatch):
    children = []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        assert kwargs["start_new_session"] is True
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = harness(Ctx(root=repo, profile="manager"), action="tidy", wait=0)
    assert result.data["detached"] is True
    assert result.data["dry_run"] is True
    assert Path(result.data["log"]).exists()
    assert Path(result.data["result"]).exists()
    assert children[0].wait(timeout=30) == 0
    import json

    assert json.loads(Path(result.data["result"]).read_text())["dry_run"] is True
