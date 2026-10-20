"""A product repo with no remote: the registry entry validates, sync leaves it alone, doctor
calls it local-only, and a dispatch starts from its local default branch."""

from pathlib import Path

import pytest

from conftest import git

GAMMA = """  - name: Gamma
    short: gamma
    path: repos/Gamma
    default_branch: main
    worktrees_dir: repos/_worktrees/gamma
    harness: "none"
    dispatch: {model: null, permission_mode: acceptEdits, max_budget_usd: 1, extra_args: []}
"""


def _add_gamma(root: Path, *, clone: bool = True) -> Path:
    reg = root / "agent.yaml"
    reg.write_text(reg.read_text(encoding="utf-8") + GAMMA, encoding="utf-8")
    gamma = root / "repos" / "Gamma"
    if clone:
        gamma.mkdir(parents=True)
        git(["init", "-q", "-b", "main", "--template="], gamma)
        git(["config", "user.email", "owner@example.com"], gamma)
        git(["config", "user.name", "Owner"], gamma)
        git(["commit", "-q", "--allow-empty", "-m", "empty"], gamma)
    return gamma


def test_an_entry_without_remote_validates(tmp_root: Path):
    from agent_cli import repos

    _add_gamma(tmp_root, clone=False)
    registry = repos.load_registry(tmp_root)
    assert registry.require("gamma").local_only
    assert registry.require("gamma").remote == ""
    assert not registry.require("alpha").local_only


@pytest.mark.parametrize("flags", [[], ["--pull"], ["--fetch"]])
def test_sync_skips_a_local_only_clone(tmp_root: Path, run, flags):
    gamma = _add_gamma(tmp_root)
    head = git(["rev-parse", "HEAD"], gamma).strip()
    r = run(["--json", "repos", "sync", "gamma", *flags])
    assert r.code == 0, r.err
    assert r.json[0]["actions"] == ["local-only (no remote; not fetched or pulled)"]
    assert r.json[0]["local_only"] is True and r.json[0]["remote_ok"] is None
    assert git(["rev-parse", "HEAD"], gamma).strip() == head
    assert "local-only" in run(["repos", "status", "gamma"]).out


def test_sync_reports_a_missing_local_only_repo_without_cloning(tmp_root: Path, run):
    gamma = _add_gamma(tmp_root, clone=False)
    r = run(["--json", "repos", "sync", "gamma", "--pull"])
    assert r.code == 0, r.err
    assert r.json[0]["actions"] == ["missing (local-only, no remote to clone from)"]
    assert r.json[0]["exists"] is False
    assert not gamma.exists()


def test_sync_of_a_repo_with_origin_is_unchanged(tmp_root: Path, run):
    _add_gamma(tmp_root)
    r = run(["--json", "repos", "sync", "alpha", "--pull"])
    assert r.code == 0, r.err
    assert r.json[0]["actions"] == ["already current"]
    assert r.json[0]["remote_ok"] is True and r.json[0]["local_only"] is False


def test_dispatch_base_is_the_local_default_branch_without_origin(tmp_root: Path):
    from agent_cli import repos
    from agent_cli.dispatch.worktrees import ensure_worktree, launch_base

    gamma = _add_gamma(tmp_root)
    git(["commit", "-q", "--allow-empty", "-m", "local only"], gamma)
    head = git(["rev-parse", "refs/heads/main"], gamma).strip()
    registry = repos.load_registry(tmp_root)
    rp = registry.require("gamma")
    base = launch_base(tmp_root, rp, None)
    assert base == head
    wt = tmp_root / "repos/_worktrees/gamma/x"
    made = ensure_worktree(tmp_root, rp, wt, "feature/x", base)
    assert made["created"] and made["base"] == head
    assert git(["rev-parse", "HEAD"], wt).strip() == head
    assert launch_base(tmp_root, registry.require("alpha"), None) == "origin/main"
    first = git(["rev-parse", "HEAD~1"], gamma).strip()
    assert launch_base(tmp_root, rp, first) == first


def test_dispatch_dry_run_shows_the_local_base(tmp_root: Path, run, fake_claude, notes_remote):
    gamma = _add_gamma(tmp_root)
    head = git(["rev-parse", "refs/heads/main"], gamma).strip()
    run(["feature", "new", "x", "--title", "X", "--repo", "gamma"])
    contract = tmp_root / "harness" / "worker-contract.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("# worker contract (test stub)\n")
    r = run(["--json", "dispatch", "new", "gamma", "--feature", "1", "--prompt", "x", "--dry-run"])
    assert r.code == 0, r.err
    assert r.json["base"] == head


def test_doctor_reports_a_local_only_repo_as_ok(tmp_root: Path):
    from agent_cli.doctor.checks_repos import check_registry

    _add_gamma(tmp_root)
    checks = {c["check"]: c for c in check_registry(tmp_root)}
    remote = checks["repo gamma remote"]
    assert remote["level"] == "ok"
    assert "local-only" in remote["detail"]
    assert "repo alpha remote" not in checks
    assert not [c for c in checks.values() if c["level"] == "fail"]


def test_doctor_says_a_missing_local_only_repo_cannot_be_cloned(tmp_root: Path):
    from agent_cli.doctor.checks_repos import check_registry

    _add_gamma(tmp_root, clone=False)
    checks = {c["check"]: c for c in check_registry(tmp_root)}
    missing = checks["repo gamma on disk"]
    assert missing["level"] == "warn"
    assert "cannot clone" in missing["detail"]
    assert "repos sync" not in missing["detail"]
