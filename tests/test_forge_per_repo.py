"""A repo's own `forge:` overrides agent.yaml's: a harness may register repos on more than one
forge, and a worker in each gets that forge's read-only rules."""

from pathlib import Path

import yaml

from agent_cli import config_file, forge
from agent_cli import dispatch as dispatch_mod
from agent_cli import repos as repos_mod
from dispatch_helpers import droot  # noqa: F401 -- a pytest fixture, used as a parameter name


def _set(root: Path, *, registry: str | None, alpha: str | None) -> repos_mod.Repo:
    path = config_file.registry_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.pop("forge", None)
    if registry:
        data["forge"] = registry
    entry = next(r for r in data["repos"] if r.get("short", r["name"]) == "alpha" or r["name"] == "alpha")
    entry.pop("forge", None)
    if alpha:
        entry["forge"] = alpha
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return repos_mod.load_registry(root).require("alpha")


def _perms(root: Path, repo: repos_mod.Repo) -> tuple[list[str], list[str]]:
    settings = dispatch_mod.build_settings(root, repo, worktree="/wt/alpha/001-x")
    return settings["permissions"]["allow"], settings["permissions"]["deny"]


def test_a_gitlab_repo_in_a_github_registry_gets_the_glab_rules(droot: Path):
    allow, deny = _perms(droot, _set(droot, registry="github", alpha="gitlab"))
    assert "Bash(glab mr view:*)" in allow
    assert "Bash(glab mr create:*)" in deny
    assert not any(rule.startswith("Bash(gh ") for rule in allow + deny), "gh's rules are replaced"


def test_a_github_repo_in_a_gitlab_registry_gets_the_gh_rules(droot: Path):
    allow, deny = _perms(droot, _set(droot, registry="gitlab", alpha="github"))
    assert any(rule.startswith("Bash(gh ") for rule in allow)
    assert not any("glab" in rule for rule in allow + deny)


def test_a_repo_without_the_key_inherits_the_registry(droot: Path):
    repo = _set(droot, registry="gitlab", alpha=None)
    assert repo.forge is None
    allow, _ = _perms(droot, repo)
    assert "Bash(glab mr view:*)" in allow


def test_for_repo_resolution_order():
    assert forge.for_repo(None, None).name == forge.DEFAULT
    assert forge.for_repo("gitlab", None).name == "gitlab"
    assert forge.for_repo("gitlab", "github").name == "github"


def test_an_unknown_repo_forge_fails_validation():
    data = {"version": 1, "repos": [{"name": "x", "path": "../repos/x", "remote": "r", "forge": "bitbucket"}]}
    assert any("repos[0] forge" in p for p in repos_mod.validate_registry_data(data))
    data["repos"][0]["forge"] = "GitLab"
    assert repos_mod.validate_registry_data(data) == [], "case-insensitive, like the top-level key"
