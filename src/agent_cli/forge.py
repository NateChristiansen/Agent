"""Configure code-hosting CLI reads and denials per forge, selected by agent.yaml and defaulting to
GitHub.

Workers can read their own change request, including inline feedback through read-only API calls.
Deny every write form before evaluating broad read allowances, including flags that turn API reads
into writes. Only the manager may publish for the owner. remote_hosts normalizes clone origins for
repository comparison.

The dispatch ledger resolves PRs only through gh and GitHub URLs; other forges retain dispatch
results without PR metadata. The git-push guard is provider-independent.
"""

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
        # reading its own pull request: a worker addressing review feedback has to see the
        # feedback. `gh pr view --comments` does not include inline review comments, so the api
        # read is the only way to get them. The deny list below blocks every write spelling.
        "Bash(gh pr view:*)",
        "Bash(gh pr diff:*)",
        "Bash(gh pr checks:*)",
        "Bash(gh pr list:*)",
        "Bash(gh pr status:*)",
        # `Bash(gh api repos/:*)` looked tighter but matched nothing: the prefix has to end on a
        # token boundary, so `gh api repos/<owner>/...` was denied. `gh api`
        # is a GET unless one of the write flags is passed, and every one of those is denied.
        "Bash(gh api:*)",
        "Bash(gh auth status:*)",
    ),
    deny=(
        "Bash(gh pr create:*)",
        "Bash(gh pr merge:*)",
        "Bash(gh repo:*)",
        # a worker never speaks for the owner on github. It fixes the code and says so in its
        # return. Replying to a review thread, resolving it, or retitling the PR is the owner's.
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
        # the api read above is scoped to repos/, but a POST goes through the same prefix, so
        # every write spelling of `gh api` is denied outright
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
        # the merge-request equivalents of the github reads above. `glab mr view` does not carry
        # inline discussion either, so the api read does the same job here.
        "Bash(glab mr view:*)",
        "Bash(glab mr diff:*)",
        "Bash(glab mr list:*)",
        "Bash(glab ci status:*)",
        "Bash(glab ci view:*)",
        "Bash(glab api:*)",
        # `glab auth status` prints the token when given `-t`, unlike `gh`, where it is a
        # separate verb. The broad read stays and the token spellings are denied below; deny is
        # checked first, so the narrow rule wins over this one.
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
        # the token, which `glab` prints from the same verb the reads use
        "Bash(glab auth status -t*)",
        "Bash(glab auth status --show-token*)",
        # every write spelling of `glab api`, the same shape as the github rules
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
    """The named forge, or the default when nothing is named."""
    if not name:
        return FORGES[DEFAULT]
    forge = FORGES.get(str(name).strip().lower())
    if forge is None:
        raise AgentError(f"unknown forge {name!r}; agent.yaml `forge:` knows: {', '.join(sorted(FORGES))}")
    return forge


def for_repo(registry_forge: str | None, repo_forge: str | None) -> Forge:
    """The forge a repo's workers are held to: the repo's own `forge:`, else agent.yaml's
    top-level `forge:`, else the default. A harness may register repos on more than one
    forge."""
    return require(repo_forge or registry_forge)
