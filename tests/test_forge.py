"""The forge seam: which code-hosting CLI a worker may read and must never write.

The rules used to be spelled `gh` in twenty-odd `BASE_DENY` entries, so the harness's central
invariant — the manager speaks on the forge, the worker never does — held on GitHub and silently
nowhere else. These tests hold both halves: that moving the github rules into data changed nothing
at all, and that the gitlab set actually denies the writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_cli import dispatch as dispatch_mod
from agent_cli import forge as forge_mod
from agent_cli import permissions as perms
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from dispatch_helpers import droot  # noqa: F401 -- a pytest fixture, used as a parameter name

# ------------------------------------------------------- github, unchanged by the move

# The acceptance test for the refactor. These are the strings that were literals in
# `dispatch/rules.py` before copied here by hand from the pre-split file. If the
# extraction dropped, reordered or reworded one of them, this fails.
GITHUB_ALLOW = [
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh pr list:*)",
    "Bash(gh pr status:*)",
    "Bash(gh api:*)",
    "Bash(gh auth status:*)",
]
GITHUB_DENY = [
    "Bash(gh pr create:*)",
    "Bash(gh pr merge:*)",
    "Bash(gh repo:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh pr review:*)",
    "Bash(gh pr edit:*)",
    "Bash(gh pr close:*)",
    "Bash(gh pr reopen:*)",
    "Bash(gh pr ready:*)",
    "Bash(gh issue:*)",
    "Bash(gh release:*)",
    "Bash(gh workflow:*)",
    "Bash(gh run:*)",
    "Bash(gh secret:*)",
    "Bash(gh auth token:*)",
    "Bash(gh api -X:*)",
    "Bash(gh api --method:*)",
    "Bash(gh api * -X *)",
    "Bash(gh api * --method *)",
    "Bash(gh api * -f *)",
    "Bash(gh api * -F *)",
    "Bash(gh api * --field *)",
    "Bash(gh api * --raw-field *)",
    "Bash(gh api * --input *)",
    "Bash(gh api graphql:*)",
]


def test_the_github_rules_are_exactly_what_they_were():
    assert list(forge_mod.GITHUB.allow) == GITHUB_ALLOW
    assert list(forge_mod.GITHUB.deny) == GITHUB_DENY


def test_the_base_lists_still_carry_the_github_rules_in_place():
    """`BASE_ALLOW` and `BASE_DENY` render the default forge, because every module-level reader
    of them — the eval corpus, `doctor`'s wiring check, preflight's error message — has no
    registry in hand. Position matters only for a human reading the list, so assert the
    neighbours rather than an index: the reads sit between the toolchain rules and `WebFetch`,
    and the denials between `git push` and `git reset --hard`."""
    allow = list(dispatch_mod.BASE_ALLOW)
    deny = list(dispatch_mod.BASE_DENY)
    for rule in GITHUB_ALLOW:
        assert rule in allow
    for rule in GITHUB_DENY:
        assert rule in deny
    assert allow.index("Bash(npx:*)") < allow.index(GITHUB_ALLOW[0])
    assert allow.index(GITHUB_ALLOW[-1]) < allow.index("WebFetch")
    assert deny.index("Bash(git push:*)") < deny.index(GITHUB_DENY[0])
    assert deny.index(GITHUB_DENY[-1]) < deny.index("Bash(git reset --hard:*)")


# ------------------------------------------------------------------ gitlab


@pytest.mark.parametrize(
    "command",
    [
        "glab mr view 42",
        "glab mr diff 42",
        "glab mr list --author @me",
        "glab ci status",
        "glab api projects/1/merge_requests/42/notes",
        "glab auth status",
    ],
)
def test_gitlab_reads_are_allowed(command: str):
    allow = list(forge_mod.GITLAB.allow)
    deny = list(forge_mod.GITLAB.deny)
    verdict, _rule = perms.matches(command, allow, deny)
    assert verdict == "allowed", command


@pytest.mark.parametrize(
    "command",
    [
        "glab mr create --title x",
        "glab mr merge 42",
        "glab mr update 42 --title y",
        "glab mr approve 42",
        "glab mr note 42 --message hi",
        "glab issue create --title x",
        "glab repo fork",
        "glab release create v1",
        "glab variable set KEY value",
        "glab ci run",
        "glab auth login",
        "glab auth status -t",
        "glab auth status --show-token",
        "glab api -X POST projects/1/merge_requests",
        "glab api --method POST projects/1/merge_requests",
        "glab api projects/1/notes -f body=hi",
    ],
)
def test_gitlab_writes_are_denied(command: str):
    """Including `glab auth status -t`, which prints the token from the same verb the reads use.
    Unlike `gh`, where the token is its own verb, this only works because deny is checked before
    allow."""
    allow = list(forge_mod.GITLAB.allow)
    deny = list(forge_mod.GITLAB.deny)
    verdict, _rule = perms.matches(command, allow, deny)
    assert verdict == "denied", command


def test_every_gitlab_rule_can_match_something():
    """The same lint a launch runs: a rule that cannot match is indistinguishable from a
    deliberate denial once the worker is headless."""
    problems = perms.lint_all(
        allow=list(forge_mod.GITLAB.allow), deny=list(forge_mod.GITLAB.deny), label="gitlab"
    )
    assert problems == []


# ------------------------------------------------------------------ selection


def test_require_defaults_to_github_and_rejects_the_unknown():
    assert forge_mod.require(None) is forge_mod.GITHUB
    assert forge_mod.require("") is forge_mod.GITHUB
    assert forge_mod.require("GitLab") is forge_mod.GITLAB
    with pytest.raises(AgentError) as exc:
        forge_mod.require("bitbucket")
    assert "github" in str(exc.value) and "gitlab" in str(exc.value)


def test_registry_reads_and_validates_the_forge_key():
    base = {"version": 1, "repos": [], "forge": "gitlab"}
    assert repos_mod.validate_registry_data(base) == []
    base["forge"] = "bitbucket"
    assert any("forge" in p for p in repos_mod.validate_registry_data(base))


def test_a_gitlab_registry_swaps_the_rules_a_worker_gets(droot: Path):
    """The whole point: with `forge: gitlab` a worker can read a merge request and cannot write
    one. Before this, its `glab` calls matched no rule at all and the deny-only fall-through
    refused them — safe, and useless."""
    reg = droot / "agent.yaml"
    data = yaml.safe_load(reg.read_text(encoding="utf-8"))
    data["forge"] = "gitlab"
    reg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    repo = repos_mod.load_registry(droot).require("alpha")
    settings = dispatch_mod.build_settings(droot, repo, worktree="/wt/alpha/001-x")
    allow = settings["permissions"]["allow"]
    deny = settings["permissions"]["deny"]
    assert "Bash(glab mr view:*)" in allow
    assert "Bash(glab mr create:*)" in deny
    assert not any("gh " in rule for rule in allow), "the github rules are gone, not appended"
    assert not any("Bash(gh " in rule for rule in deny)
    # the non-forge rules are untouched
    assert "Bash(git push:*)" in deny
    assert "WebFetch" in allow


def test_same_remote_normalises_both_forges():
    assert repos_mod._same_remote("git@gitlab.com:org/thing.git", "https://gitlab.com/org/thing")
    assert repos_mod._same_remote("git@github.com:org/thing.git", "https://github.com/org/thing")
    assert not repos_mod._same_remote("git@gitlab.com:org/thing.git", "git@gitlab.com:org/other.git")
