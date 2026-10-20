# ruff: noqa
from __future__ import annotations
from pathlib import Path
import yaml
from .. import repos as repos_mod
from ..core import AgentError
from ..repos_validate import registry_unknown_keys
from .check import Check, new_check


def check_registry(root: Path) -> list[Check]:
    out: list[Check] = closed_leftovers(root)
    try:
        registry = repos_mod.load_registry(root)
    except AgentError as exc:
        return [*out, new_check("agent.yaml", "fail", exc.message)]
    out.append(new_check("agent.yaml", "ok", f"{len(registry.repos)} repo(s)"))
    data = yaml.safe_load(repos_mod.registry_path(root).read_text(encoding="utf-8"))
    out.extend((new_check("agent.yaml unknown key", "warn", p) for p in registry_unknown_keys(data)))
    for repo in registry.repos:
        path = repo.abs_path(root)
        if not repo.worktrees_dir:
            out.append(
                new_check(
                    f"repo {repo.short} worktrees_dir",
                    "warn",
                    "null; dispatch falls back to <clones>/_worktrees/<short>. Set it in agent.yaml",
                )
            )
        if not (path / ".git").exists():
            placeholder = "your-org" in (repo.remote or "") or repo.short == "example-service"
            if placeholder:
                detail = f"{repo.path} is the example entry from agent.example.yaml — replace it with a repo you have (`/agent-config repo` adds one), or delete it"
            elif repo.local_only:
                detail = f"{path} is missing{_source(repo)}; the entry has no remote, so sync cannot clone it. Fix its path"
            else:
                detail = f"{path} not cloned{_source(repo)}; run `agent repos sync`"
            out.append(
                new_check(
                    f"repo {repo.short} in agent.yaml" if placeholder else f"repo {repo.short} on disk",
                    "warn",
                    detail,
                )
            )
            continue
        out.append(new_check(f"repo {repo.short} path", "ok", f"{path}{_source(repo)}"))
        info = repos_mod.repo_status(root, repo)
        if repo.local_only:
            out.append(
                new_check(
                    f"repo {repo.short} remote",
                    "ok",
                    f"local-only (no remote in agent.yaml): sync does not fetch it, dispatch starts from local {repo.default_branch} when the clone has no origin",
                )
            )
        elif info["remote_ok"] is False:
            out.append(new_check(f"repo {repo.short} remote", "warn", f"origin does not match {repo.remote}"))
        for w in info["worktrees"]:
            if w["prunable"]:
                out.append(new_check(f"repo {repo.short} worktree", "warn", f"{w['path']} is prunable"))
        out += _diff_rule_checks(repo, path)
    return out


def closed_leftovers(root: Path) -> list[Check]:
    from .. import tidy

    try:
        listing = tidy.inventory(root)
    except AgentError, OSError:
        return []
    out = [
        new_check(
            "closed-feature worktree",
            "warn",
            f"{row['path']}: {'; '.join(row['holds']) or 'safe to remove'}; run agent tidy --dry-run",
        )
        for row in listing["trees"]
    ]
    out += [
        new_check("closed-feature branch", "warn", f"{row['branch']} is resolved; run agent tidy --dry-run")
        for row in listing["branches"]
        if row["resolved"]
    ]
    return out


def _source(repo: repos_mod.Repo) -> str:
    return " (from agent.local.yaml)" if "path" in repo.local else ""


def _unmark(text: str) -> str:
    return " ".join(text.replace("**", "").replace("__", "").split())


def _diff_rule_checks(repo: repos_mod.Repo, clone: Path) -> list[Check]:
    try:
        rules = repo.diff_rules
    except ValueError as exc:
        return [new_check(f"repo {repo.short} diff_rules", "fail", str(exc))]
    out: list[Check] = []
    for rule in rules:
        doc = clone / rule.doc
        if not doc.is_file():
            out.append(
                new_check(f"repo {repo.short} diff_rules", "fail", f"{rule.doc} does not exist in the clone")
            )
        elif _unmark(rule.rule) not in _unmark(doc.read_text(encoding="utf-8", errors="replace")):
            out.append(
                new_check(f"repo {repo.short} diff_rules", "warn", f'{rule.doc} no longer says "{rule.rule}"')
            )
    if rules and (not out):
        out.append(
            new_check(
                f"repo {repo.short} diff_rules", "ok", f"{len(rules)} rule(s), each quote found in its doc"
            )
        )
    return out
