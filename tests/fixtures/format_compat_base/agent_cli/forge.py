# ruff: noqa
from __future__ import annotations
from dataclasses import dataclass
from .core import AgentError


@dataclass(frozen=True)
class Forge:
    name: str
    cli: str
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    remote_hosts: tuple[str, ...]


GITHUB = Forge(
    name="github",
    cli="gh",
    allow=(
        "Bash(gh pr view:*)",
        "Bash(gh pr diff:*)",
        "Bash(gh pr checks:*)",
        "Bash(gh pr list:*)",
        "Bash(gh pr status:*)",
        "Bash(gh api:*)",
        "Bash(gh auth status:*)",
    ),
    deny=(
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
    ),
    remote_hosts=("github.com",),
)
GITLAB = Forge(
    name="gitlab",
    cli="glab",
    allow=(
        "Bash(glab mr view:*)",
        "Bash(glab mr diff:*)",
        "Bash(glab mr list:*)",
        "Bash(glab ci status:*)",
        "Bash(glab ci view:*)",
        "Bash(glab api:*)",
        "Bash(glab auth status:*)",
    ),
    deny=(
        "Bash(glab mr create:*)",
        "Bash(glab mr merge:*)",
        "Bash(glab mr update:*)",
        "Bash(glab mr close:*)",
        "Bash(glab mr reopen:*)",
        "Bash(glab mr approve:*)",
        "Bash(glab mr unapprove:*)",
        "Bash(glab mr note:*)",
        "Bash(glab mr todo:*)",
        "Bash(glab mr rebase:*)",
        "Bash(glab mr revoke:*)",
        "Bash(glab repo:*)",
        "Bash(glab issue:*)",
        "Bash(glab release:*)",
        "Bash(glab variable:*)",
        "Bash(glab schedule:*)",
        "Bash(glab snippet:*)",
        "Bash(glab label:*)",
        "Bash(glab incident:*)",
        "Bash(glab deploy-key:*)",
        "Bash(glab ssh-key:*)",
        "Bash(glab ci run:*)",
        "Bash(glab ci trigger:*)",
        "Bash(glab ci retry:*)",
        "Bash(glab ci cancel:*)",
        "Bash(glab ci delete:*)",
        "Bash(glab auth login:*)",
        "Bash(glab auth status -t*)",
        "Bash(glab auth status --show-token*)",
        "Bash(glab api -X:*)",
        "Bash(glab api --method:*)",
        "Bash(glab api * -X *)",
        "Bash(glab api * --method *)",
        "Bash(glab api * -f *)",
        "Bash(glab api * -F *)",
        "Bash(glab api * --field *)",
        "Bash(glab api * --raw-field *)",
        "Bash(glab api * --input *)",
    ),
    remote_hosts=("gitlab.com",),
)
FORGES = {f.name: f for f in (GITHUB, GITLAB)}
DEFAULT = GITHUB.name


def require(name: str | None) -> Forge:
    if not name:
        return FORGES[DEFAULT]
    forge = FORGES.get(str(name).strip().lower())
    if forge is None:
        raise AgentError(f"unknown forge {name!r}; agent.yaml `forge:` knows: {', '.join(sorted(FORGES))}")
    return forge


def for_repo(registry_forge: str | None, repo_forge: str | None) -> Forge:
    return require(repo_forge or registry_forge)
