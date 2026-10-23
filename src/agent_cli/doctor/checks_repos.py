"""Check repository configuration and whether each diff rule still quotes its governing document."""

from __future__ import annotations

from pathlib import Path

import yaml

from .. import gitgate
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
    out.extend(new_check("agent.yaml unknown key", "warn", p) for p in registry_unknown_keys(data))
    own = _own_repo(root)
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
            # The tracked `agent.example.yaml` ships one entry pointing at
            # `git@github.com:your-org/example-service.git`. Telling a newcomer to run
            # `repos sync` against it sends them at a remote that cannot resolve, which is the
            # first thing a fresh clone did. The example stays, because it
            # documents the shape of an entry; the advice changes.
            placeholder = "your-org" in (repo.remote or "") or repo.short == "example-service"
            # "on disk" read as if the example were cloned.
            if placeholder:
                detail = (
                    f"{repo.path} is the example entry from agent.example.yaml — replace it with "
                    "a repo you have (`/agent-config repo` adds one), or delete it"
                )
            elif repo.local_only:
                detail = (
                    f"{path} is missing{_source(repo)}; the entry has no remote, so sync cannot "
                    "clone it. Fix its path"
                )
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
        out += _review_hook_check(root, repo, path, own)
        info = repos_mod.repo_status(root, repo)
        if repo.local_only:
            out.append(
                new_check(
                    f"repo {repo.short} remote",
                    "ok",
                    "local-only (no remote in agent.yaml): sync does not fetch it, dispatch starts "
                    f"from local {repo.default_branch} when the clone has no origin",
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
            "finished worktree",
            "warn",
            f"{row['path']}: {'; '.join(row['holds']) or 'safe to remove'}; run agent tidy --dry-run",
        )
        for row in listing["trees"]
    ]
    count = sum(1 for row in listing["branches"] if row["resolved"] and not row.get("checked_out"))
    if count:
        out.append(
            new_check(
                "resolved branches",
                "warn",
                f"{count} resolved branches without checkouts; run agent tidy --dry-run",
            )
        )
    return out


_REVIEW_HOOK = {
    "installed": ("ok", "installed"),
    "missing": ("fail", "missing, so pushes from it skip the code-review gate; run `agent setup`"),
    "stale": ("fail", "out of date; run `agent setup`"),
    "foreign": (
        "fail",
        "foreign hook present (someone else's pre-push, or a core.hooksPath the repo manages), "
        "left in place; the code-review gate does not run there",
    ),
}


def _own_repo(root: Path) -> Path | None:
    """The harness checkout's git common dir. Setup gives that repository the full gate, not the
    review hook; a product clone that shares only its hooks dir is still a product clone."""
    if not (root / ".git").exists():
        return None
    try:
        return gitgate.common_dir(root)
    except AgentError, OSError, ValueError:
        return None


def _review_hook_check(root: Path, repo: repos_mod.Repo, clone: Path, own: Path | None) -> list[Check]:
    """The clone's pre-push code-review hook (ADR-0063), as `gitgate.inspect_review` reads it."""
    name = f"repo {repo.short} pre-push review hook"
    try:
        if own is not None and gitgate.common_dir(clone) == own:
            return []
        state = gitgate.inspect_review(clone, root)
    except (AgentError, OSError, ValueError) as exc:
        return [new_check(name, "fail", f"cannot read it: {exc}")]
    level, detail = _REVIEW_HOOK[state]
    return [new_check(name, level, detail)]


def _source(repo: repos_mod.Repo) -> str:
    """Where a repo's path came from, when this machine's agent.local.yaml overrode it."""
    return " (from agent.local.yaml)" if "path" in repo.local else ""


def _unmark(text: str) -> str:
    """Doc text with markdown emphasis and runs of whitespace removed, so a quoted rule
    matches the sentence whether or not the doc bolds it."""
    return " ".join(text.replace("**", "").replace("__", "").split())


def _diff_rule_checks(repo: repos_mod.Repo, clone: Path) -> list[Check]:
    """Check that every convention document exists in the clone and still contains its quoted rule.
    Otherwise workers could be blocked by wording the repo no longer holds.
    """
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
    if rules and not out:
        out.append(
            new_check(
                f"repo {repo.short} diff_rules", "ok", f"{len(rules)} rule(s), each quote found in its doc"
            )
        )
    return out
