# ruff: noqa
from __future__ import annotations
import math
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml
from . import forge as forge_mod
from .config_file import PLACEHOLDER_OWNER, apply_local, owner_name, registry_path
from .core import AgentError, ExternalFailure, NotFound
from .records.narrow import as_dict, as_dict_or_none, as_float, as_int, as_list, as_list_or_none, dict_list

__all__ = ["PLACEHOLDER_OWNER", "owner_name"]
WORKTREES_PARENT = "_worktrees"
DEFAULT_TIMEOUT_MINUTES = 45
DEFAULT_MAX_TURNS = 200
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")
DEFAULT_BRANCH_TEMPLATE = "feature/{slug}{suffix}"
BRANCH_TEMPLATE_FIELDS = ("number", "slug", "ticket", "suffix")


def dispatch_bool(dispatch: Mapping[str, object], key: str, default: bool = False) -> bool:
    value = dispatch.get(key, default)
    if not isinstance(value, bool):
        raise AgentError(f"dispatch.{key} must be a boolean")
    return value


@dataclass(frozen=True)
class VerificationCommand:
    command: str
    timeout_seconds: float


@dataclass(frozen=True)
class VerificationConfig:
    commands: tuple[VerificationCommand, ...]
    timeout_seconds: float


def verification_config(dispatch: Mapping[str, object]) -> VerificationConfig | None:
    if "verify" not in dispatch and "verify_timeout_seconds" not in dispatch:
        return None

    def bound(value: object, key: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a finite positive number of seconds")
        try:
            result = float(value)
        except OverflowError as exc:
            raise ValueError(f"{key} must be a finite positive number of seconds") from exc
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{key} must be a finite positive number of seconds")
        return result

    total = bound(dispatch.get("verify_timeout_seconds"), "dispatch.verify_timeout_seconds")
    rows = as_list_or_none(dispatch.get("verify"))
    if not rows:
        raise ValueError("dispatch.verify must be a non-empty list of command/timeout_seconds mappings")
    commands: list[VerificationCommand] = []
    for index, raw in enumerate(rows):
        key = f"dispatch.verify[{index}]"
        row = as_dict_or_none(raw)
        if row is None or set(row) != {"command", "timeout_seconds"}:
            raise ValueError(f"{key} must contain exactly command and timeout_seconds")
        command = row["command"]
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError(f"{key}.command must be a non-empty shell command without NUL")
        timeout = bound(row["timeout_seconds"], f"{key}.timeout_seconds")
        if timeout > total:
            raise ValueError(f"{key}.timeout_seconds must not exceed dispatch.verify_timeout_seconds")
        commands.append(VerificationCommand(command, timeout))
    return VerificationConfig(tuple(commands), total)


DIFF_RULE_CHANGES = ("added", "modified", "any")
DIFF_RULE_KEYS = {"paths", "change", "forbid", "doc", "rule"}


@dataclass(frozen=True)
class DiffRule:
    paths: str
    change: str
    forbid: str
    doc: str
    rule: str


def diff_rules(dispatch: Mapping[str, object]) -> list[DiffRule]:
    given = dispatch.get("diff_rules")
    rows = as_list_or_none(given) if given else []
    if rows is None:
        raise ValueError("dispatch.diff_rules must be a list of rule mappings")
    rules: list[DiffRule] = []
    for index, raw in enumerate(rows):
        key = f"dispatch.diff_rules[{index}]"
        row = as_dict_or_none(raw)
        if row is None or set(row) != DIFF_RULE_KEYS:
            raise ValueError(f"{key} must contain exactly {', '.join(sorted(DIFF_RULE_KEYS))}")
        values: dict[str, str] = {}
        for k in DIFF_RULE_KEYS:
            v = row[k]
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{key}.{k} must be a non-empty string")
            values[k] = v
        if values["change"] not in DIFF_RULE_CHANGES:
            raise ValueError(f"{key}.change must be one of {', '.join(DIFF_RULE_CHANGES)}")
        try:
            re.compile(values["forbid"])
        except re.error as exc:
            raise ValueError(f"{key}.forbid is not a valid regex: {exc}") from exc
        rules.append(DiffRule(**values))
    return rules


@dataclass
class Repo:
    name: str
    short: str
    path: str
    remote: str
    default_branch: str = "main"
    worktrees_dir: str | None = None
    harness: str = ""
    dispatch: dict[str, object] = field(default_factory=dict[str, object])
    notes: str = ""
    local: tuple[str, ...] = ()
    forge: str | None = None

    @property
    def local_only(self) -> bool:
        return not self.remote

    def abs_path(self, root: Path) -> Path:
        return (root / self.path).resolve()

    def abs_worktrees(self, root: Path) -> Path | None:
        return (root / self.worktrees_dir).resolve() if self.worktrees_dir else None

    @property
    def allowed_tools(self) -> list[str]:
        return [str(t) for t in as_list(self.dispatch.get("allowed_tools"))]

    @property
    def diff_rules(self) -> list[DiffRule]:
        return diff_rules(self.dispatch)

    @property
    def verification(self) -> VerificationConfig | None:
        return verification_config(self.dispatch)

    @property
    def timeout_minutes(self) -> float:
        value = self.dispatch.get("timeout_minutes")
        return as_float(value, float(DEFAULT_TIMEOUT_MINUTES))

    @property
    def max_turns(self) -> int:
        value = self.dispatch.get("max_turns")
        return as_int(value, DEFAULT_MAX_TURNS)

    @property
    def branch_template(self) -> str:
        value = self.dispatch.get("branch_template")
        return str(value) if value else DEFAULT_BRANCH_TEMPLATE

    @property
    def env_allow(self) -> list[str]:
        return [str(name) for name in as_list(self.dispatch.get("env_allow"))]

    @property
    def repo_mcp(self) -> list[str] | None:
        value = self.dispatch.get("repo_mcp")
        return None if value is None else [str(s) for s in as_list(value)]


@dataclass
class Registry:
    version: int
    repos: list[Repo]
    forge: str = forge_mod.DEFAULT
    "Which code-hosting CLI a worker may read and must never write with (`agent_cli/forge.py`).\n    Defaults to github, so an existing registry keeps the rules it had."

    def find(self, ref: str) -> Repo | None:
        ref_l = ref.strip().lower()
        for r in self.repos:
            if r.name.lower() == ref_l or r.short.lower() == ref_l:
                return r
        return None

    def require(self, ref: str) -> Repo:
        repo = self.find(ref)
        if repo is None:
            raise NotFound(f"unknown repo {ref!r}; see `agent repos list`")
        return repo


def _registry_version(value: object) -> int:
    if isinstance(value, str) and value.isdigit() or (isinstance(value, float) and value.is_integer()):
        return int(value)
    return as_int(value, 1)


def load_registry(root: Path) -> Registry:
    path = registry_path(root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or dict[str, object]()
    except FileNotFoundError as exc:
        raise AgentError(
            f"{path} is missing: `agent setup` links it in from your notes repo, creating it from agent.example.yaml if the notes repo has none"
        ) from exc
    except yaml.YAMLError as exc:
        raise AgentError(f"{path}: invalid YAML: {exc}") from exc
    problems = validate_registry_data(data, unknown_keys=False)
    if problems:
        raise AgentError(f"{path}: " + "; ".join(problems))
    data = as_dict(data)
    repos: list[Repo] = []
    for r in dict_list(data["repos"]):
        worktrees_dir = r.get("worktrees_dir")
        repos.append(
            Repo(
                name=str(r["name"]),
                short=str(r.get("short") or r["name"]),
                path=str(r["path"]),
                remote=str(r.get("remote") or ""),
                default_branch=str(r.get("default_branch") or "main"),
                worktrees_dir=worktrees_dir if isinstance(worktrees_dir, str) else None,
                harness=str(r.get("harness") or ""),
                dispatch=dict(as_dict(r.get("dispatch"))),
                notes=str(r.get("notes") or ""),
                forge=str(r["forge"]).strip().lower() if r.get("forge") else None,
            )
        )
    repos = apply_local(root, repos)
    return Registry(
        version=_registry_version(data.get("version", 1)),
        repos=repos,
        forge=str(data.get("forge") or forge_mod.DEFAULT).strip().lower(),
    )


def validate_registry_data(data: Any, *, unknown_keys: bool = True) -> list[str]:
    from .repos_validate import validate_registry_data as _validate

    return _validate(data, unknown_keys=unknown_keys)


def run_git(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="surrogateescape"
        )
    except FileNotFoundError as exc:
        raise ExternalFailure("git is not installed") from exc
    if r.returncode != 0:
        raise ExternalFailure(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()}")
    return r.stdout


def git_or_none(args: list[str], cwd: Path) -> str | None:
    try:
        return run_git(args, cwd)
    except ExternalFailure:
        return None


def worktrees(path: Path, strict: bool = False) -> list[dict[str, Any]]:
    out = (run_git if strict else git_or_none)(["worktree", "list", "--porcelain"], path)
    if not out:
        return []
    items: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                items.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "branch": None, "prunable": False, "bare": False}
        elif key == "branch":
            current["branch"] = value.replace("refs/heads/", "")
        elif key in ("prunable", "bare"):
            current[key] = True
        elif key == "detached":
            current["branch"] = "(detached)"
    if current:
        items.append(current)
    return items


def repo_status(root: Path, repo: Repo, fetch: bool = False) -> dict[str, Any]:
    path = repo.abs_path(root)
    info: dict[str, Any] = {
        "name": repo.name,
        "short": repo.short,
        "path": str(path),
        "exists": path.is_dir() and (path / ".git").exists(),
        "branch": None,
        "ahead": None,
        "behind": None,
        "dirty": None,
        "remote_ok": None,
        "local_only": repo.local_only,
        "worktrees": [],
    }
    if not info["exists"]:
        return info
    if fetch and (not repo.local_only):
        run_git(["fetch", "--quiet", "origin"], path)
    info["branch"] = (git_or_none(["rev-parse", "--abbrev-ref", "HEAD"], path) or "").strip() or None
    counts = git_or_none(
        ["rev-list", "--left-right", "--count", f"HEAD...origin/{repo.default_branch}"], path
    )
    if counts:
        ahead, behind = counts.split()
        info["ahead"], info["behind"] = (int(ahead), int(behind))
    porcelain = git_or_none(["status", "--porcelain"], path)
    info["dirty"] = len(porcelain.splitlines()) if porcelain is not None else None
    if not repo.local_only:
        origin = (git_or_none(["remote", "get-url", "origin"], path) or "").strip()
        info["remote_ok"] = _same_remote(origin, repo.remote) if origin else False
    info["worktrees"] = [w for w in worktrees(path) if Path(w["path"]).resolve() != path]
    return info


def _same_remote(a: str, b: str, hosts: tuple[str, ...] | None = None) -> bool:
    prefixes = tuple(
        (
            f"{scheme}{host}{sep}"
            for host in hosts or tuple((h for f in forge_mod.FORGES.values() for h in f.remote_hosts))
            for scheme, sep in (("git@", ":"), ("https://", "/"), ("ssh://git@", "/"))
        )
    )

    def norm(url: str) -> str:
        url = url.strip().lower()
        for prefix in prefixes:
            if url.startswith(prefix):
                url = url[len(prefix) :]
        return url.removesuffix(".git").strip("/")

    return norm(a) == norm(b)


def has_origin(path: Path) -> bool | None:
    out = git_or_none(["remote"], path)
    return None if out is None else "origin" in out.split()


def pull(root: Path, repo: Repo) -> str:
    path = repo.abs_path(root)
    branch = (git_or_none(["rev-parse", "--abbrev-ref", "HEAD"], path) or "").strip()
    if branch != repo.default_branch:
        return f"not pulled (on {branch or 'a detached HEAD'}, not {repo.default_branch})"
    porcelain = git_or_none(["status", "--porcelain"], path)
    if porcelain:
        return f"not pulled ({len(porcelain.splitlines())} uncommitted change(s))"
    behind = git_or_none(["rev-list", "--count", f"HEAD..origin/{repo.default_branch}"], path)
    if behind is None:
        return f"not pulled (no origin/{repo.default_branch})"
    if behind.strip() == "0":
        return "already current"
    run_git(["merge", "--ff-only", f"origin/{repo.default_branch}"], path)
    return f"pulled ({behind.strip()} commit(s))"


def sync(
    root: Path, repo: Repo, no_clone: bool = False, fetch: bool = False, pull_default: bool = False
) -> dict[str, Any]:
    path = repo.abs_path(root)
    actions: list[str] = []
    if not (path / ".git").exists():
        if repo.local_only:
            actions.append("missing (local-only, no remote to clone from)")
        elif no_clone:
            actions.append("missing (not cloned; --no-clone)")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            run_git(["clone", repo.remote, str(path)], path.parent)
            actions.append("cloned")
    wt_dir = repo.abs_worktrees(root)
    if (path / ".git").exists() and wt_dir and wt_dir.is_dir():
        try:
            registrations = worktrees(path, strict=True)
            parent_path = wt_dir.resolve()
            registered = [
                candidate
                for item in registrations
                if (candidate := Path(str(item["path"])).resolve()).is_dir()
                and any((ancestor.samefile(parent_path) for ancestor in candidate.parents))
            ]
        except (ExternalFailure, OSError) as exc:
            raise ExternalFailure(f"worktree registration discovery failed for {path}: {exc}") from exc
        for candidate in sorted(registered):
            try:
                run_git(["worktree", "repair", str(candidate)], path)
            except ExternalFailure as exc:
                raise ExternalFailure(f"registered worktree repair failed for {candidate}: {exc}") from exc
        actions.extend([f"worktree repair ({len(registered)})"] if registered else [])
    if repo.local_only and (path / ".git").exists():
        actions.append("local-only (no remote; not fetched or pulled)")
        fetch = pull_default = False
    if pull_default and (path / ".git").exists():
        run_git(["fetch", "--quiet", "origin"], path)
        actions.append(pull(root, repo))
        fetch = False
    status = repo_status(root, repo, fetch=fetch) if (path / ".git").exists() else repo_status(root, repo)
    status["actions"] = actions
    return status


def all_worktree_paths(root: Path, registry: Registry) -> list[tuple[Repo, Path]]:
    out: list[tuple[Repo, Path]] = []
    for repo in registry.repos:
        main = repo.abs_path(root)
        if (main / ".git").exists():
            out.append((repo, main))
            for w in worktrees(main):
                p = Path(w["path"]).resolve()
                if p != main and p.is_dir():
                    out.append((repo, p))
    return out
