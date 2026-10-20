# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any, Literal
from .. import forge as forge_mod
from .. import launcher, permissions, worker_mcp
from .. import repos as repos_mod
from ..core import AgentError

BASE_ALLOW = [
    "Edit",
    "Write",
    "MultiEdit",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git blame:*)",
    "Bash(git rev-parse:*)",
    "Bash(git ls-files:*)",
    "Bash(git ls-tree:*)",
    "Bash(git cat-file:*)",
    "Bash(git stash list:*)",
    "Bash(git grep:*)",
    "Bash(git merge-base:*)",
    "Bash(git rev-list:*)",
    "Bash(git range-diff:*)",
    "Bash(git diff-tree:*)",
    "Bash(git describe:*)",
    "Bash(git shortlog:*)",
    "Bash(git name-rev:*)",
    "Bash(git remote -v)",
    "Bash(git remote show:*)",
    "Bash(git remote get-url:*)",
    "Bash(git fetch:*)",
    "Bash(git merge:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git rm:*)",
    "Bash(git mv:*)",
    "Bash(git restore:*)",
    "Bash(git checkout --:*)",
    "Bash(git branch)",
    "Bash(git branch:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(grep:*)",
    "Bash(rg:*)",
    "Bash(find:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(diff:*)",
    "Bash(awk:*)",
    "Bash(sort:*)",
    "Bash(uniq:*)",
    "Bash(cut:*)",
    "Bash(tr:*)",
    "Bash(which:*)",
    "Bash(pwd)",
    "Bash(echo:*)",
    "Bash(mkdir:*)",
    "Bash(touch:*)",
    "Bash(cp:*)",
    "Bash(mv:*)",
    "Bash(rm:*)",
    "Bash(chmod:*)",
    "Bash(python3:*)",
    "Bash(python:*)",
    "Bash(.venv/bin/python:*)",
    "Bash(.venv/Scripts/python.exe:*)",
    "Bash(pip:*)",
    "Bash(pip3:*)",
    "Bash(pytest:*)",
    "Bash(uv:*)",
    "Bash(node:*)",
    "Bash(npx:*)",
    *forge_mod.require(None).allow,
    "WebFetch",
    "WebSearch",
    "mcp__agent__log",
]
BASE_DENY = [
    "Bash(git push)",
    "Bash(git push:*)",
    *forge_mod.require(None).deny,
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(git branch -D:*)",
    "Bash(git worktree:*)",
    "Bash(git * --force*)",
    "Bash(git * -f *)",
    "Bash(rm -r*)",
    "Bash(rm -R*)",
    "Bash(rm -f*)",
    "Bash(rm -*r*)",
    "Bash(rm -*R*)",
    "Bash(rm -*f*)",
    "Bash(find * -delete*)",
    "Bash(claude)",
    "Bash(claude:*)",
]
GIT_READ_VERBS = (
    "status",
    "diff",
    "log",
    "show",
    "blame",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "cat-file",
    "grep",
    "merge-base",
    "rev-list",
    "range-diff",
    "diff-tree",
    "describe",
    "shortlog",
    "name-rev",
)
GIT_VERBS = (
    *GIT_READ_VERBS,
    "add",
    "commit",
    "branch",
    "rm",
    "mv",
    "restore",
    "fetch",
    "merge",
    "checkout --",
)


def _forge_swapped(
    base: list[str], root: Path, side: Literal["allow", "deny"], repo: repos_mod.Repo | None = None
) -> list[str]:
    default = forge_mod.require(None)
    try:
        configured = forge_mod.for_repo(repos_mod.load_registry(root).forge, repo.forge if repo else None)
    except Exception:
        return list(base)
    if configured.name == default.name:
        return list(base)
    old = getattr(default, side)
    new = getattr(configured, side)
    out: list[str] = []
    spliced = False
    for rule in base:
        if rule in old:
            if not spliced:
                out.extend(new)
                spliced = True
            continue
        out.append(rule)
    if not spliced:
        out.extend(new)
    return out


def registry_repos(root: Path) -> list[repos_mod.Repo]:
    try:
        return list(repos_mod.load_registry(root).repos)
    except Exception:
        return []


def selected_servers(repo: repos_mod.Repo, profiles: list[worker_mcp.Profile]) -> list[str]:
    return [*(repo.repo_mcp or []), *(prof.server for prof in profiles)]


def selection_problems(repo: repos_mod.Repo, profiles: list[worker_mcp.Profile]) -> list[str]:
    from .mcp_policy import _label

    hide = selected_servers(repo, profiles)
    problems = permissions.mcp_selection_problems(repo.repo_mcp or [])
    for prof in profiles:
        where = f"dispatch.mcp profile {_label(prof.name)!r}"
        problems += permissions.mcp_selection_problems(
            [prof.server], where, "drop the profile from dispatch.mcp"
        )
        problems += permissions.mcp_profile_problems(prof.server, prof.allow, prof.deny, where, hide)
    own = list(repo.allowed_tools)
    allow = [*BASE_ALLOW, *own, *(rule for prof in profiles for rule in prof.allow)]
    for server in dict.fromkeys(hide):
        problems += permissions.mcp_grant_problems(
            server, allow, own, f"agent.yaml {repo.short} allowed_tools", hide
        )
    return problems


def base_policy(root: Path, repo: repos_mod.Repo | None) -> tuple[list[str], list[str]]:
    allow = _forge_swapped(BASE_ALLOW, root, "allow", repo)
    deny = _forge_swapped(BASE_DENY, root, "deny", repo) + [
        f"Edit(//{root}/**)",
        f"Write(//{root}/**)",
        f"MultiEdit(//{root}/**)",
    ]
    return (allow, deny)


def build_settings(
    root: Path, repo: repos_mod.Repo, worktree: str | None = None, *, mcp: list[str] | tuple[str, ...] = ()
) -> dict[str, Any]:
    allow, deny = base_policy(root, repo)
    profiles = worker_mcp.require(root, mcp)
    for rule in (rule for prof in profiles for rule in prof.allow):
        if rule not in allow:
            allow.append(rule)
    if worktree:
        for verb in GIT_VERBS:
            allow.append(f"Bash(git -C {worktree} {verb}:*)")
    for other in registry_repos(root):
        clone = other.abs_path(root)
        if worktree and str(clone) == worktree:
            continue
        for verb in GIT_READ_VERBS:
            rule = f"Bash(git -C {clone} {verb}:*)"
            if rule not in allow:
                allow.append(rule)
    for tool in repo.allowed_tools:
        if tool not in allow:
            allow.append(tool)
        if worktree and tool.startswith("Bash(git ") and (not tool.startswith("Bash(git -C ")):
            twin = f"Bash(git -C {worktree} {tool[len('Bash(git ') :]}"
            if twin not in allow:
                allow.append(twin)
    for rule in (rule for prof in profiles for rule in prof.deny):
        if rule not in deny:
            deny.append(rule)
    problems = selection_problems(repo, profiles) + permissions.lint_all(
        allow=allow, deny=deny, label=f"{repo.short} worker", hide=selected_servers(repo, profiles)
    )
    if problems:
        raise AgentError(
            "permission rules are broken; fix them before dispatching:\n  " + "\n  ".join(problems)
        )
    command, prefix = launcher.command_and_args(root)
    precompact = {"type": "command", "command": " ".join([f'"{command}"', *prefix, "hook", "precompact"])}
    return {
        "permissions": {"allow": allow, "deny": deny},
        "hooks": {"PreCompact": [{"hooks": [precompact]}]},
        "crossSessionInbound": "accept",
        "autoMemoryEnabled": False,
        "attribution": {"commit": "", "pr": "", "sessionUrl": False},
    }
