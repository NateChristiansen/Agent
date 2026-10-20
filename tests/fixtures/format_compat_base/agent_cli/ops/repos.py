# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml
from .. import repos as repos_mod
from .. import sessions
from ..core import AgentError, table
from ..repos_validate import validate_registry_data
from ._shared import Op


def repos_validate(root: Path) -> Op:
    path = repos_mod.registry_path(root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AgentError(f"{path}: unreadable or invalid YAML: {exc}") from exc
    problems = validate_registry_data(data)
    if not problems:
        repos_mod.load_registry(root)
    return Op(
        {"ok": not problems, "problems": problems},
        "\n".join(problems) if problems else "agent.yaml is valid",
        code=1 if problems else 0,
    )


def repos_list(root: Path) -> Op:
    registry = repos_mod.load_registry(root)
    rows: list[list[str]] = []
    data: list[dict[str, Any]] = []
    for r in registry.repos:
        exists = (r.abs_path(root) / ".git").exists()
        rows.append([r.name, r.short, r.path, "yes" if exists else "no", r.default_branch])
        data.append(
            {
                "name": r.name,
                "short": r.short,
                "path": r.path,
                "exists": exists,
                "default_branch": r.default_branch,
                "worktrees_dir": r.worktrees_dir,
                "dispatch": r.dispatch,
            }
        )
    return Op(data, table(rows, ["name", "short", "path", "cloned", "branch"]))


def _status_rows(infos: list[dict[str, Any]]) -> str:
    rows: list[list[str]] = []
    for i in infos:
        if not i["exists"]:
            rows.append([i["short"], "(not cloned)", "", "", "", ""])
            continue
        ab = f"+{i['ahead']}/-{i['behind']}" if i["ahead"] is not None else "?"
        rows.append(
            [
                i["short"],
                i["branch"] or "?",
                ab,
                str(i["dirty"]),
                "local-only" if i.get("local_only") else "ok" if i["remote_ok"] else "MISMATCH",
                str(len(i["worktrees"])),
            ]
        )
        for w in i["worktrees"]:
            rows.append(["  ↳", w["branch"] or "?", "", "", "prunable" if w["prunable"] else "", w["path"]])
    return table(rows, ["repo", "branch", "ahead/behind", "dirty", "remote", "worktrees"])


def repos_status(root: Path, *, name: str | None = None, fetch: bool = False) -> Op:
    registry = repos_mod.load_registry(root)
    targets = [registry.require(name)] if name else registry.repos
    infos = [repos_mod.repo_status(root, r, fetch=fetch) for r in targets]
    return Op(infos, _status_rows(infos))


def repos_sync(
    root: Path,
    *,
    name: str | None = None,
    no_clone: bool = False,
    fetch: bool = False,
    pull: bool = False,
    harness: bool = False,
) -> Op:
    if harness:
        if name or no_clone or fetch or pull:
            raise AgentError("harness=true syncs the harness's own main; it takes no other option")
        from .. import main_sync

        done = main_sync.fast_forward(root)
        return Op(done, f"harness main: {done['action']} ({done['old'][:12]} -> {done['new'][:12]})")
    registry = repos_mod.load_registry(root)
    targets = [registry.require(name)] if name else registry.repos
    infos = [repos_mod.sync(root, r, no_clone=no_clone, fetch=fetch, pull_default=pull) for r in targets]
    if pull:
        _record_pulled(root, [str(i["short"]) for i in infos])
    lines = [f"{i['short']}: {', '.join(i['actions']) or 'up to date'}" for i in infos]
    return Op(infos, "\n".join(lines) + "\n\n" + _status_rows(infos))


PULLED_KEY = "pulled"


def _record_pulled(root: Path, shorts: list[str]) -> None:
    try:
        sid = sessions.resolve_session_id(root)
    except AgentError:
        return
    for short in shorts:
        sessions.add_to_session_list(root, sid, PULLED_KEY, short)
