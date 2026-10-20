"""The harness config (agent.yaml, once repos.yaml): the repo registry, and git-backed status/sync."""

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
from .config_file import PLACEHOLDER_OWNER, apply_local, owner_name, registry_path  # noqa: F401 — re-exported
from .core import AgentError, ExternalFailure, NotFound
from .records.narrow import as_dict, as_dict_or_none, as_float, as_int, as_list, as_list_or_none, dict_list

# Names imported only so callers can reach them as `repos.<name>`.
__all__ = ["PLACEHOLDER_OWNER", "owner_name"]

WORKTREES_PARENT = "_worktrees"
DEFAULT_TIMEOUT_MINUTES = 45
# Third runaway-worker bound beside budget and wall clock; 200 exceeds prior completed runs.
DEFAULT_MAX_TURNS = 200
# Keep SDK literals in step; hooks and status checks avoid importing the launch SDK.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")

# `agent/<feature>` did not match repo conventions; templates use feature/worktree fields.
DEFAULT_BRANCH_TEMPLATE = "feature/{slug}{suffix}"
BRANCH_TEMPLATE_FIELDS = ("number", "slug", "ticket", "suffix")


def dispatch_bool(dispatch: Mapping[str, object], key: str, default: bool = False) -> bool:
    """Config switches accept only YAML booleans, including when a caller constructs a Repo."""
    value = dispatch.get(key, default)
    if not isinstance(value, bool):
        raise AgentError(f"dispatch.{key} must be a boolean")
    return value


@dataclass(frozen=True)
class VerificationCommand:
    """A runnable shell command and its explicit wall-clock bound, in seconds."""

    command: str
    timeout_seconds: float


@dataclass(frozen=True)
class VerificationConfig:
    """Ordered supervisor checks, separate from worker-reported required-check regexes."""

    commands: tuple[VerificationCommand, ...]
    timeout_seconds: float


def verification_config(dispatch: Mapping[str, object]) -> VerificationConfig | None:
    """Parse strict verification config without importing the worker/SDK package.

    Omission is legacy/unconfigured, never an exemption. Explicit configuration requires
    both a nonempty `verify` list and `verify_timeout_seconds`; no bound is inferred from
    the worker's budget or timeout. Raises ValueError for invalid configuration.
    """
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
    """One convention a repo's own doc states, held on the diff at return.

    `paths` is an fnmatch glob from the repo root (`*` crosses `/`), `change` which files it
    governs (`added`, `modified` or `any`), `forbid` a regex no added line may match, `doc` the
    repo file that states the convention, and `rule` the sentence quoted from it. `doctor`
    checks the quote is still in the doc, so a rule cannot outlive the convention it cites."""

    paths: str
    change: str
    forbid: str
    doc: str
    rule: str


def diff_rules(dispatch: Mapping[str, object]) -> list[DiffRule]:
    """Parse `dispatch.diff_rules`. Raises ValueError on a malformed rule."""
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
    local: tuple[str, ...] = ()  # keys set by this machine's agent.local.yaml (config_file.py)
    forge: str | None = None  # this repo's forge; None inherits agent.yaml `forge:` (forge.for_repo)

    @property
    def local_only(self) -> bool:
        """No `remote` in agent.yaml: sync never clones or fetches it."""
        return not self.remote

    def abs_path(self, root: Path) -> Path:
        return (root / self.path).resolve()

    def abs_worktrees(self, root: Path) -> Path | None:
        return (root / self.worktrees_dir).resolve() if self.worktrees_dir else None

    @property
    def allowed_tools(self) -> list[str]:
        """Permission strings a dispatched worker may use on top of the standard set."""
        return [str(t) for t in as_list(self.dispatch.get("allowed_tools"))]

    @property
    def diff_rules(self) -> list[DiffRule]:
        """Conventions held on a worker's diff at return, `[]` when the repo declares none."""
        return diff_rules(self.dispatch)

    @property
    def verification(self) -> VerificationConfig | None:
        """Validated supervisor commands, or None for an unconfigured legacy registry."""
        return verification_config(self.dispatch)

    @property
    def timeout_minutes(self) -> float:
        """Unchecked; `dispatch new` uses `repos_validate.resolve_timeout`, which also names the source."""
        value = self.dispatch.get("timeout_minutes")
        return as_float(value, float(DEFAULT_TIMEOUT_MINUTES))

    @property
    def max_turns(self) -> int:
        """Turn cap for a worker in this repo (`DEFAULT_MAX_TURNS` when the repo declares
        none). A turn is a user message plus the assistant response it draws."""
        value = self.dispatch.get("max_turns")
        return as_int(value, DEFAULT_MAX_TURNS)

    @property
    def branch_template(self) -> str:
        """The branch name a dispatch derives in this repo, as a format string over `number`,
        `slug`, `ticket` (the first ticket id, lowercased, or empty) and `suffix` (`-02` for a
        second concurrent worktree, else empty). The default is `feature/<slug>`; a repo that names
        branches another way sets `dispatch.branch_template`."""
        value = self.dispatch.get("branch_template")
        return str(value) if value else DEFAULT_BRANCH_TEMPLATE

    @property
    def env_allow(self) -> list[str]:
        """Environment variable names, or `FOO_*` prefixes, a worker in this repo inherits on
        top of `dispatch.options.WORKER_ENV_BASE`.

        The base is deliberately a minimal POSIX set, because core ships public and a toolchain
        list is not generic: one stack wants `UV_*`, another `PNPM_HOME`, and a repo
        cloned for something else wants neither. Naming them per repo also keeps the grant
        legible — the dry run prints what a worker will get. Unlike `dispatch.env`, which sets a
        value, this only lets an existing one through."""
        return [str(name) for name in as_list(self.dispatch.get("env_allow"))]

    @property
    def repo_mcp(self) -> list[str] | None:
        """Explicit product MCP selections, or `None` for an absent/null registry value.

        Worker launches always use strict MCP configuration. `None` and an empty list both
        exclude product servers; only named selections join the harness and selected profiles.
        """
        value = self.dispatch.get("repo_mcp")
        return None if value is None else [str(s) for s in as_list(value)]


@dataclass
class Registry:
    version: int
    repos: list[Repo]
    forge: str = forge_mod.DEFAULT
    """Which code-hosting CLI a worker may read and must never write with (`agent_cli/forge.py`).
    Defaults to github, so an existing registry keeps the rules it had."""

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
    if isinstance(value, str) and value.isdigit() or isinstance(value, float) and value.is_integer():
        return int(value)
    return as_int(value, 1)


def load_registry(root: Path) -> Registry:
    path = registry_path(root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or dict[str, object]()
    except FileNotFoundError as exc:
        # The config is the owner's, not the harness's (see ADR-0012): it is gitignored here and
        # linked in from the notes repo, which setup creates from the tracked example.
        raise AgentError(
            f"{path} is missing: `agent setup` links it in from your notes repo, creating it "
            f"from agent.example.yaml if the notes repo has none"
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
    """Problems with an agent.yaml, or `[]`. Implemented in `repos_validate.py`; imported here so the name every caller already uses keeps
    working. The import is in the body because that module reads this one's constants."""
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
    if fetch and not repo.local_only:
        run_git(["fetch", "--quiet", "origin"], path)
    info["branch"] = (git_or_none(["rev-parse", "--abbrev-ref", "HEAD"], path) or "").strip() or None
    counts = git_or_none(
        ["rev-list", "--left-right", "--count", f"HEAD...origin/{repo.default_branch}"], path
    )
    if counts:
        ahead, behind = counts.split()
        info["ahead"], info["behind"] = int(ahead), int(behind)
    porcelain = git_or_none(["status", "--porcelain"], path)
    info["dirty"] = len(porcelain.splitlines()) if porcelain is not None else None
    if not repo.local_only:
        origin = (git_or_none(["remote", "get-url", "origin"], path) or "").strip()
        info["remote_ok"] = _same_remote(origin, repo.remote) if origin else False
    info["worktrees"] = [w for w in worktrees(path) if Path(w["path"]).resolve() != path]
    return info


def _same_remote(a: str, b: str, hosts: tuple[str, ...] | None = None) -> bool:
    """Whether two remote URLs name the same repo, ignoring how they are spelled.

    The host prefixes come from the forge (`agent_cli/forge.py`) rather than being hardcoded to
    github, so a GitLab clone compares equal to its registry entry too. Every known host is
    stripped, not only the configured one: a clone may legitimately have been cloned over HTTPS
    from one and SSH from another during a migration, and this is a comparison, not a check."""
    prefixes = tuple(
        f"{scheme}{host}{sep}"
        for host in (hosts or tuple(h for f in forge_mod.FORGES.values() for h in f.remote_hosts))
        for scheme, sep in (("git@", ":"), ("https://", "/"), ("ssh://git@", "/"))
    )

    def norm(url: str) -> str:
        url = url.strip().lower()
        for prefix in prefixes:
            if url.startswith(prefix):
                url = url[len(prefix) :]
        return url.removesuffix(".git").strip("/")

    return norm(a) == norm(b)


def has_origin(path: Path) -> bool | None:
    """Whether the clone has a remote named `origin`; None when git did not answer, which is
    not proof that it has none."""
    out = git_or_none(["remote"], path)
    return None if out is None else "origin" in out.split()


def pull(root: Path, repo: Repo) -> str:
    """Fast-forward the main clone's default branch to origin. Never merges, never rebases,
    never touches a dirty tree, and never touches a branch that is not the default one. The
    clone exists so the manager reads current truth; anything else is the owner's working state.

    Returns a one-phrase description of what happened, for the sync `actions` list.
    """
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
    root: Path,
    repo: Repo,
    no_clone: bool = False,
    fetch: bool = False,
    pull_default: bool = False,
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
                and any(ancestor.samefile(parent_path) for ancestor in candidate.parents)
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
    # A pull is only meaningful against a fresh origin ref, so it implies the fetch.
    if pull_default and (path / ".git").exists():
        run_git(["fetch", "--quiet", "origin"], path)
        actions.append(pull(root, repo))
        fetch = False
    status = repo_status(root, repo, fetch=fetch) if (path / ".git").exists() else repo_status(root, repo)
    status["actions"] = actions
    return status


def all_worktree_paths(root: Path, registry: Registry) -> list[tuple[Repo, Path]]:
    """Every checkout the journal should scan: main clones plus their worktrees."""
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
