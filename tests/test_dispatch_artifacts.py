"""A builder's artifact keeps its worktree until it lands or is abandoned, and a harness dispatch
starts from locally landed main.

Every review once reused the builder's clean, finished worktree and moved it to the
review branch, so `dispatch resume` of the builder was refused and each fix became a fresh
dispatch. Harness dispatches also started from `origin/main`, far behind local main."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import worktrees
from conftest import git
from dispatch_helpers import (
    _new_fast as new_fast,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)
from dispatch_helpers import (
    droot,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture, used as a parameter name
    envelope,
    one,
    settle,
)

MAIN = "repos/alpha-api"


def _commit(worktree: Path, name: str) -> str:
    (worktree / name).write_text(f"{name}\n", encoding="utf-8")
    git(["add", name], worktree)
    git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name], worktree)
    return str(git(["rev-parse", "HEAD"], worktree)).strip()


def test_a_review_does_not_take_an_unlanded_builders_worktree(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope()), one(envelope())])
    build: dict[str, Any] = new_fast(droot)
    _commit(Path(build["worktree"]), "built.txt")
    settle(droot, build)
    review: dict[str, Any] = new_fast(droot)
    assert review["worktree"] != build["worktree"] and review["reused"] is False
    head = str(git(["rev-parse", "--abbrev-ref", "HEAD"], Path(build["worktree"]))).strip()
    assert head == build["branch"], "the builder's worktree stays on its branch, so findings can resume it"


def test_a_dispatch_onto_the_builders_branch_continues_in_its_worktree(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope()), one(envelope())])
    build: dict[str, Any] = new_fast(droot)
    _commit(Path(build["worktree"]), "built.txt")
    settle(droot, build)
    fix: dict[str, Any] = new_fast(droot, branch=build["branch"])
    assert fix["worktree"] == build["worktree"] and fix["reused"] is True


def test_a_landed_artifact_frees_its_worktree(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope()), one(envelope())])
    build: dict[str, Any] = new_fast(droot)
    _commit(Path(build["worktree"]), "built.txt")
    git(["merge", "-q", "--ff-only", build["branch"]], droot / MAIN)
    settle(droot, build)
    nxt: dict[str, Any] = new_fast(droot)
    assert nxt["worktree"] == build["worktree"] and nxt["reused"] is True


def test_a_builder_resumed_after_landing_owns_its_new_work(droot: Path, fake_claude: Any):
    """Codex's phase-1 review: an earlier landing note must not free a builder that kept working."""
    fake_claude.scenario([one(envelope()), one(envelope())])
    build: dict[str, Any] = new_fast(droot)
    _commit(Path(build["worktree"]), "built.txt")
    git(["merge", "-q", "--ff-only", build["branch"]], droot / MAIN)
    _commit(Path(build["worktree"]), "more.txt")
    record = {**build, "integration": {"state": "landed"}}
    repo = repos_mod.Repo(name="alpha-api", short="alpha", path=MAIN, remote="x")
    assert worktrees.artifact_open(droot, repo, record) is True
    settle(droot, build)
    nxt: dict[str, Any] = new_fast(droot)
    assert nxt["worktree"] != build["worktree"]


def test_a_squash_landed_tip_frees_its_worktree_and_later_work_keeps_it(droot: Path, fake_claude: Any):
    """Codex's round-2 review: a squash landing puts a new commit on main, so the builder's tip is
    never an ancestor of main; the recorded landed tip releases it, a later commit does not."""
    fake_claude.scenario([one(envelope())])
    build: dict[str, Any] = new_fast(droot)
    tip = _commit(Path(build["worktree"]), "built.txt")
    repo = repos_mod.Repo(name="alpha-api", short="alpha", path=MAIN, remote="x")
    landed = {**build, "integration": {"state": "landed", "tip": tip}}
    assert worktrees.artifact_open(droot, repo, landed) is False
    _commit(Path(build["worktree"]), "more.txt")
    assert worktrees.artifact_open(droot, repo, landed) is True


def test_an_abandoned_artifact_frees_its_worktree(droot: Path):
    repo = repos_mod.Repo(name="alpha-api", short="alpha", path=MAIN, remote="x")
    record = {"state": "abandoned", "branch": "no-such-branch"}
    assert worktrees.artifact_open(droot, repo, record) is False


def _repo_with_origin_behind(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "harness"
    repo.mkdir()

    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout

    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    old = run("rev-parse", "HEAD").strip()
    run("update-ref", "refs/remotes/origin/main", old)
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two")
    return repo, old, run("rev-parse", "HEAD").strip()


def test_a_harness_dispatch_starts_from_local_main_pinned(tmp_path: Path):
    root, origin_sha, local_sha = _repo_with_origin_behind(tmp_path)
    harness = repos_mod.Repo(name="Harness", short="harness", path=".", remote="x")
    assert worktrees.launch_base(root, harness, None) == local_sha != origin_sha


def test_a_product_repo_keeps_its_remote_default(droot: Path):
    product = repos_mod.Repo(name="alpha-api", short="alpha", path=MAIN, remote="x")
    assert worktrees.launch_base(droot, product, None) == "origin/main"


def test_an_explicit_base_that_does_not_resolve_is_refused(tmp_path: Path):
    root, _, local_sha = _repo_with_origin_behind(tmp_path)
    harness = repos_mod.Repo(name="Harness", short="harness", path=".", remote="x")
    assert worktrees.launch_base(root, harness, local_sha) == local_sha
    assert worktrees.launch_base(root, harness, "main") == local_sha, "a symbolic base is pinned too"
    assert worktrees.launch_base(root, harness, "origin/somewhere") == "origin/somewhere"
    with pytest.raises(AgentError, match="not a commit"):
        worktrees.launch_base(root, harness, "deadbeef" * 5)


def test_review_target_is_pinned_and_explicit_base_wins(tmp_path: Path):
    root, old, artifact = _repo_with_origin_behind(tmp_path)
    repo = repos_mod.Repo(name="Harness", short="harness", path=".", remote="x")
    git(["branch", "artifact", artifact], root)
    pinned = worktrees.launch_base(root, repo, None, row="review", target="artifact")
    git(["branch", "-f", "artifact", old], root)
    assert pinned == artifact
    assert worktrees.launch_base(root, repo, artifact, row="review", target="artifact") == artifact
    assert worktrees.launch_base(root, repo, None, row="build", target="artifact") == artifact
    with pytest.raises(AgentError, match="review target"):
        worktrees.launch_base(root, repo, None, row="review", target="missing-artifact")


def test_review_dispatch_checks_out_the_target_artifact(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope()), one(envelope())])
    build = new_fast(droot)
    artifact = _commit(Path(build["worktree"]), "artifact.txt")
    review = new_fast(droot, row="review", target=build["branch"], no_checks=True, budget_reason="review row")
    assert review["base"] == artifact
    assert review["writes"] == [], "an omitted `writes` on a review reserves nothing"
    checkout = Path(review["worktree"])
    assert str(git(["rev-parse", "HEAD"], checkout)).strip() == artifact
    assert (checkout / "artifact.txt").read_text(encoding="utf-8") == "artifact.txt\n"


def test_a_review_with_no_target_and_no_base_is_refused(droot: Path, fake_claude: Any):
    """A review cut from main instead of the build tip reviewed the wrong code (L-20261015-07)."""
    with pytest.raises(AgentError, match="a review needs `target`"):
        new_fast(droot, row="review", no_checks=True, budget_reason="review row")


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_review_target_passes_through_the_frontends(droot: Path, surface: str, capsys):
    from agent_cli import cli
    from agent_cli.core import Output
    from agent_cli.mcp.specs_dispatch import dispatch as dispatch_tool
    from agent_cli.mcp.tools import Ctx

    expected = str(git(["rev-parse", "HEAD"], droot / MAIN)).strip()
    if surface == "cli":
        args = cli.parse(
            [
                "--json",
                "dispatch",
                "new",
                "alpha",
                "--feature",
                "1",
                "--row",
                "review",
                "--target",
                "main",
                "--dry-run",
                "--no-checks",
                "--reason",
                "review row",
                "--prompt",
                "Review the artifact.",
            ]
        )
        assert cli.dispatch(droot, args, Output(as_json=True)) == 0
        result = json.loads(capsys.readouterr().out)
    else:
        result = dispatch_tool(
            Ctx(root=droot, profile="manager"),
            "new",
            repo="alpha",
            feature="1",
            session_id="manager",
            row="review",
            target="main",
            dry_run=True,
            no_checks=True,
            budget_reason="review row",
            prompt="Review the artifact.",
        ).data
    assert result["base"] == expected
