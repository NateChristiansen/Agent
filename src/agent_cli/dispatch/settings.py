"""Build worker permission rules and settings files.

This module owns base allow/deny lists and safe git `-C` twins. `rules.py` owns runtime callbacks
and MCP configuration. Its re-exports and callers' function-local imports preserve public names and
monkeypatch lookup at call time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .. import forge as forge_mod
from .. import launcher, permissions, worker_mcp
from .. import repos as repos_mod
from ..core import AgentError

BASE_ALLOW = [
    "Read",
    "Glob",
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
    "Bash(git check-ignore:*)",
    # `ls-tree` reads a tree at a ref without touching the working copy. A read-only role
    # uses it to inspect a repo it must not check out. Its absence was once the largest
    # single cause of denials in a run. Most came from subagents, so they never
    # showed in the top-level transcript.
    "Bash(git ls-tree:*)",
    "Bash(git cat-file:*)",
    "Bash(git stash list:*)",
    # The read-only verbs the records show a worker reaching for and being denied: `grep` 27
    # times, `merge-base` 12, `rev-list` 3, `range-diff` once — 45 denials across 31 dispatches,
    # each one a lost turn on a command that reads and nothing else. `grep` is the
    # big one: a worker searching the repo it was sent to had to fall back to `rg`, which does
    # not know about refs. `remote` is spelled out per read subcommand on purpose, because bare
    # `git remote:*` would also allow `add`, `set-url` and `remove`.
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
    # a branch that is already open as a PR moves under the worker: someone clicks "update
    # branch" and the remote gains a merge the worktree does not have.
    # Fetching and merging its own branch is how it catches up. Pushing stays denied.
    "Bash(git fetch:*)",
    "Bash(git merge:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git rm:*)",
    "Bash(git mv:*)",
    # Permit path-only restore and checkout so workers can undo generated edits. Keep branch
    # checkout unlisted: moving HEAD can orphan work and headless workers cannot answer approvals.
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
    # read-only text filters, so a worker can count and sort what it reads. sed stays out
    # because sed -i writes. Without them preflight refuses a self-check's awk|sort.
    "Bash(awk:*)",
    "Bash(sort:*)",
    "Bash(uniq:*)",
    "Bash(cut:*)",
    "Bash(tr:*)",
    "Bash(which:*)",
    "Bash(agent --help)",
    "Bash(pwd)",
    "Bash(echo:*)",
    "Bash(mkdir:*)",
    "Bash(touch:*)",
    "Bash(cp:*)",
    "Bash(mv:*)",
    "Bash(rm:*)",  # single-file removes; the deny list below still blocks -r/-f spellings
    "Bash(chmod:*)",
    # a worker writes code, so it needs its language's tooling. It starved
    # on `python3 -m venv` and `pip`.
    "Bash(python3:*)",
    "Bash(python:*)",
    # The worktree's own interpreter: `uv run python -c` crashes inside a Codex worker's sandbox
    # on macOS, and this is the same grant under another spelling.
    "Bash(.venv/bin/python:*)",
    "Bash(.venv/Scripts/python.exe:*)",  # the same interpreter in a Windows worktree
    "Bash(pip:*)",
    "Bash(pip3:*)",
    "Bash(pytest:*)",
    "Bash(uv:*)",
    "Bash(node:*)",
    "Bash(npx:*)",
    # Forge rules come from configuration so workers may read change requests but never speak on
    # the forge. These constants render the default; build_settings swaps in the repository forge.
    *forge_mod.require(None).allow,
    "WebFetch",
    "WebSearch",
    "mcp__agent__log",
]
BASE_DENY = [
    "Bash(git push)",
    "Bash(git push:*)",
    # `git -C <worktree> push` and other option spellings are refused by the worker hooks
    # (`hooks/worker_push.py`), which parse the git verb. The old wildcard `Bash(git * push*)`
    # also denied every commit whose message said " push".
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


# ------------------------------------------------------------------ worker files


# The subset safe to point at a repo the worker was NOT dispatched to: reads only, no ref
# movement, no index, no network. Every one of these also gets a `git -C <path>` twin, which is
# 80 of the 204 recorded denials on its own: a worker reads with the `-C` spelling far
# more often than the bare one, because its own worktree is not the only repo it can see.
GIT_READ_VERBS = (
    "status",
    "diff",
    "log",
    "show",
    "blame",
    "rev-parse",
    "ls-files",
    "check-ignore",
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
    """`base` with the default forge's rules replaced by the dispatched repo's forge, in place:
    the repo's own `forge:`, else agent.yaml's (`forge.for_repo`).

    `BASE_ALLOW` and `BASE_DENY` render the default forge, because every other reader of them —
    the eval corpus, `doctor`'s wiring check, preflight's error message — is a module-level
    consumer with no registry in hand. A dispatch does have one, so this is where the swap
    happens. Position is preserved rather than appended: the rules read in a deliberate order
    and a reviewer comparing this list to the old constants should see the same shape.

    Unreadable registry means the default forge, for the same reason `registry_repos` shrugs:
    permission construction must not be the thing that fails a dispatch."""
    default = forge_mod.require(None)
    try:
        configured = forge_mod.for_repo(repos_mod.load_registry(root).forge, repo.forge if repo else None)
    except Exception:  # noqa: BLE001 — a broken registry is repos_mod's error to report
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
    """Registered repos, or none if the registry cannot be read. Permission construction must
    not be the thing that fails a dispatch."""
    try:
        return list(repos_mod.load_registry(root).repos)
    except Exception:  # noqa: BLE001 — a broken registry is repos_mod's error to report
        return []


def selected_servers(repo: repos_mod.Repo, profiles: list[worker_mcp.Profile]) -> list[str]:
    """Every MCP server a dispatch selects: the repo's `repo_mcp` and its `--mcp` profiles'."""
    return [*(repo.repo_mcp or []), *(prof.server for prof in profiles)]


def selection_problems(repo: repos_mod.Repo, profiles: list[worker_mcp.Profile]) -> list[str]:
    """`permissions.mcp_selection_problems` for every selected server, whichever source selects
    it, `permissions.mcp_profile_problems` for each selected profile's MCP rules, and
    `permissions.mcp_grant_problems` for the repo's own `allowed_tools`: a repo rule naming a
    selected server some other way refuses, alone or beside a valid grant from the base list, a
    profile or the repo."""
    from .mcp_policy import _label  # pyright: ignore[reportPrivateUsage]

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
    """The allow and deny lists every dispatch for `repo` starts from, before its profiles, its
    worktree and the repo's own rules: the base lists with the repo's forge swapped in, and no edit
    to the harness root. With no repo, agent.yaml's forge."""
    allow = _forge_swapped(BASE_ALLOW, root, "allow", repo)
    deny = _forge_swapped(BASE_DENY, root, "deny", repo) + [
        f"Edit(//{root}/**)",
        f"Write(//{root}/**)",
        f"MultiEdit(//{root}/**)",
    ]
    return allow, deny


def build_settings(
    root: Path,
    repo: repos_mod.Repo,
    worktree: str | None = None,
    *,
    mcp: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    allow, deny = base_policy(root, repo)
    # a dispatch launched with `--mcp <profile>` gets that profile's servers, and the allow
    # rules the profile names for them (worker_mcp.py)
    profiles = worker_mcp.require(root, mcp)
    for rule in (rule for prof in profiles for rule in prof.allow):
        if rule not in allow:
            allow.append(rule)
    if worktree:
        # workers tend to write `git -C <worktree> ...` even from inside it. The prefix rules
        # `Bash(git status:*)` do not match that spelling, so allow the
        # exact-path form for the same verbs.
        for verb in GIT_VERBS:
            allow.append(f"Bash(git -C {worktree} {verb}:*)")
    # Workers read sibling repositories through per-clone git -C grants because their
    # working-directory guard excludes direct filesystem access. Grant read verbs only; never let a
    # worker commit outside its assigned repo.
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
        # A repo's extra git rule gets the same `git -C <worktree>` twin the base verbs get.
        # Without it `Bash(git reset --soft:*)` never matched a worker's `git -C <worktree>
        # reset --soft …`, so a rewrite the owner had authorized was denied.
        # The twin is pinned to this dispatch's worktree, so it grants no other repo anything.
        if worktree and tool.startswith("Bash(git ") and not tool.startswith("Bash(git -C "):
            twin = f"Bash(git -C {worktree} {tool[len('Bash(git ') :]}"
            if twin not in allow:
                allow.append(twin)
    # a profile may withhold single tools of a server it allows whole (a browser's evaluate)
    for rule in (rule for prof in profiles for rule in prof.deny):
        if rule not in deny:
            deny.append(rule)
    # A rule that cannot match is indistinguishable from a deliberate denial once the worker
    # is headless, and it has cost whole attempts. Refuse to launch instead. So is a selected
    # server whose rules could equally grant another server's tools, and a profile or repo MCP rule
    # the two providers read differently.
    problems = selection_problems(repo, profiles) + permissions.lint_all(
        allow=allow, deny=deny, label=f"{repo.short} worker", hide=selected_servers(repo, profiles)
    )
    if problems:
        raise AgentError(
            "permission rules are broken; fix them before dispatching:\n  " + "\n  ".join(problems)
        )
    # Run Stop and SessionEnd as SDK callbacks to avoid a fresh interpreter on every turn. Keep
    # PreCompact as a command hook: it restores the nonce and return shape from the dispatch record
    # via AGENT_DISPATCH_ID. Losing that nonce is terminal, while command re-execution costs only
    # one call per compaction; the SDK callback path lacks a live compaction check.
    command, prefix = launcher.command_and_args(root)
    precompact = {"type": "command", "command": " ".join([f'"{command}"', *prefix, "hook", "precompact"])}
    return {
        "permissions": {"allow": allow, "deny": deny},
        "hooks": {"PreCompact": [{"hooks": [precompact]}]},
        # Let the owner reach a running worker directly. Paired with `--name <dispatch-id>` in
        # build_argv, this makes `@<dispatch-id>` from their own terminal land in the worker as a user
        # turn between tool calls, instead of them having to relay through the manager or wait
        # for the return. Without it the worker binds its inbox socket but drops what arrives.
        "crossSessionInbound": "accept",
        # Same rule as the manager's own settings: nothing durable lives in ~/.claude. On by
        # default, auto memory had a worker writing notes under
        # ~/.claude/projects/<worktree slug>/memory/, keyed by a path that changes per
        # dispatch. What a worker learns goes through the `log` tool or nowhere.
        "autoMemoryEnabled": False,
        # No Co-Authored-By trailer, Claude-Session link or PR footer. The object form works on
        # every client; the bare `false` is skipped, with the whole file, before v2.1.281.
        "attribution": {"commit": "", "pr": "", "sessionUrl": False},
    }
