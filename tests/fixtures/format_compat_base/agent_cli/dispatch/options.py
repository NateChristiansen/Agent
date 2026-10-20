# ruff: noqa
from __future__ import annotations
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from claude_agent_sdk import ClaudeAgentOptions
from .. import repos as repos_mod
from ..core import AgentError, ExternalFailure, atomic_write
from ..launch_flags import parse_extra_args
from ..records.narrow import as_dict, as_int, dig
from . import mcp_policy
from . import rubric as rubric_mod
from .env_names import claude_named, env_allowed, with_platform_names
from .return_schema import RETURN_SCHEMA
from .state import WORKER_CONTRACT, file
from .telemetry_events import strip_routing

WORKER_MAX_BUFFER_BYTES = 16 * 1024 * 1024


@dataclass
class _WorkerOptions(ClaudeAgentOptions):
    mcp_selection: dict[str, Any] = field(default_factory=dict[str, Any], repr=False)


def write_worker_files(
    root: Path, d: dict[str, Any], repo: repos_mod.Repo, settings: dict[str, Any] | None = None
) -> mcp_policy.ResolvedMcp:
    from .rules import build_mcp_config, build_settings

    mcp = mcp_policy.resolve(root, repo, d)
    if settings is None:
        settings = build_settings(root, repo, worktree=d.get("worktree"), mcp=d.get("mcp") or ())
    atomic_write(file(root, d["id"], ".settings.json"), json.dumps(settings, indent=2) + "\n")
    atomic_write(
        file(root, d["id"], ".mcp.json"),
        json.dumps(build_mcp_config(root, d, repo, mcp=mcp), indent=2) + "\n",
    )
    atomic_write(file(root, d["id"], ".schema.json"), json.dumps(RETURN_SCHEMA, indent=2) + "\n")
    return mcp


def claude_on_path() -> str:
    cli = shutil.which("claude")
    if not cli:
        raise ExternalFailure(
            "`claude` is not on PATH; a worker runs the PATH claude, not the SDK's bundled CLI"
        )
    return cli


def build_options(
    root: Path,
    d: dict[str, Any],
    repo: repos_mod.Repo,
    *,
    resume: bool,
    settings: dict[str, Any] | None = None,
    mcp: mcp_policy.ResolvedMcp | None = None,
) -> ClaudeAgentOptions:
    from .rules import build_settings, sdk_hooks
    from .supervisor import permission_callback

    extra_args = _repo_extra_args(repo)
    if mcp is None:
        mcp = mcp_policy.resolve(root, repo, d)
    if settings is None:
        settings = build_settings(root, repo, worktree=d.get("worktree"), mcp=d.get("mcp") or ())
    perms = settings["permissions"]
    kwargs: dict[str, Any] = {
        "cwd": str(d["worktree"]),
        "cli_path": claude_on_path(),
        "permission_mode": str(repo.dispatch.get("permission_mode") or "acceptEdits"),
        "allowed_tools": list(perms["allow"]),
        "disallowed_tools": list(perms["deny"]),
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": (root / WORKER_CONTRACT).read_text(encoding="utf-8"),
        },
        "mcp_servers": mcp.config()["mcpServers"],
        "strict_mcp_config": True,
        "hooks": sdk_hooks(root, Path(d["worktree"]), d.get("start_sha"), str(d["id"])),
        "can_use_tool": permission_callback(root, d, repo),
        "output_format": {"type": "json_schema", "schema": RETURN_SCHEMA},
        "env": worker_env(root, d, repo),
        "settings": str(file(root, d["id"], ".settings.json")),
        "add_dirs": [str(root / d["feature_dir"])],
        "max_budget_usd": float(d["budget_usd"]),
        "max_buffer_size": WORKER_MAX_BUFFER_BYTES,
        "extra_args": {"name": d["id"], **extra_args},
    }
    kwargs["max_turns"] = int(d.get("max_turns") or repo.max_turns)
    sources = setting_sources_for(root, repo)
    if sources is not None:
        kwargs["setting_sources"] = sources
    if resume:
        kwargs["resume"] = d["session_id"]
    else:
        kwargs["session_id"] = d["session_id"]
        forked = d.get("forked_from_session")
        if forked:
            kwargs["resume"] = str(forked)
            kwargs["fork_session"] = True
    if d.get("model"):
        kwargs["model"] = str(d["model"])
    if d.get("effort"):
        kwargs["effort"] = str(d["effort"])
    return _WorkerOptions(**kwargs, mcp_selection=mcp.preview())


def _harness_self(root: Path, repo: repos_mod.Repo) -> bool:
    from ..worktree import main_checkout

    try:
        return main_checkout(repo.abs_path(root)).resolve() == main_checkout(root).resolve()
    except AgentError, ExternalFailure, OSError:
        return False


def setting_sources_for(root: Path, repo: repos_mod.Repo) -> list[str] | None:
    return ["user"] if _harness_self(root, repo) else None


def _repo_extra_args(repo: repos_mod.Repo) -> dict[str, str | None]:
    try:
        return parse_extra_args(repo.dispatch.get("extra_args"))
    except ValueError as exc:
        raise AgentError(str(exc)) from None


def _repo_mcp_file(cwd: str | Path | None, clone: str | Path | None) -> dict[str, Any] | None:
    directory = Path(cwd) if cwd else None
    if (directory is None or not directory.is_dir()) and clone:
        directory = Path(clone)
    if directory is None:
        return {}
    try:
        data = json.loads((directory / ".mcp.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError, json.JSONDecodeError:
        return None
    return {str(k): v for k, v in as_dict(dig(data, "mcpServers")).items()}


def repo_mcp_servers(cwd: str | Path | None, clone: str | Path | None = None) -> list[str]:
    servers = _repo_mcp_file(cwd, clone)
    return ["(unreadable .mcp.json)"] if servers is None else sorted(servers)


def _mcp_preview(options: ClaudeAgentOptions) -> dict[str, Any]:
    if not isinstance(options, _WorkerOptions):
        return {"strict_mcp_config": options.strict_mcp_config}
    sources = options.mcp_selection["servers"]
    return {
        "strict_mcp_config": options.strict_mcp_config,
        "mcp_servers": sorted((source["server"] for source in sources)),
        "mcp_selection": options.mcp_selection,
        "repo mcp servers": sorted(
            (
                source["server"]
                for source in sources
                if source["definition_source"].startswith(("worktree:", "clone:"))
            )
        ),
        "repo mcp policy": "explicit selection only; unselected sources not inventoried",
    }


CODEX_TURN_NOTE = (
    "not applicable to codex: Claude counts conversation turns; Codex is capped by max_tool_items"
)
DEFAULT_MAX_TOOL_ITEMS = 400


def max_tool_items(d: dict[str, Any], repo: repos_mod.Repo) -> int:
    return int(d.get("max_tool_items") or as_int(repo.dispatch.get("max_tool_items"), DEFAULT_MAX_TOOL_ITEMS))


def launch_options_preview(
    root: Path, d: dict[str, Any], repo: repos_mod.Repo, *, settings: Any
) -> dict[str, Any]:
    if d.get("provider") == "codex":
        return codex_options_preview(d, repo, resume=False, root=root)
    return options_preview(build_options(root, d, repo, resume=False, settings=settings), repo.abs_path(root))


def codex_options_preview(
    d: dict[str, Any], repo: repos_mod.Repo, *, resume: bool, root: Path | None = None
) -> dict[str, Any]:
    from .codex_policy import policy_for

    return {
        "provider": "codex",
        "cwd": str(d.get("worktree") or ""),
        "model": d.get("model"),
        "effort": d.get("effort"),
        "resume": resume,
        **policy_for(d, repo, root=root).preview(),
        "budget": d["budget_usd"],
        "max_turns": CODEX_TURN_NOTE,
        "max_tool_items": max_tool_items(d, repo),
        "message_only_progress": "bounded by timeout_minutes and the budget",
    }


def options_preview(options: ClaudeAgentOptions, clone: str | Path | None = None) -> dict[str, Any]:
    return {
        "cli_path": str(options.cli_path),
        "cwd": str(options.cwd),
        "model": options.model,
        "effort": options.effort,
        "max_budget_usd": options.max_budget_usd,
        "max_turns": options.max_turns,
        "permission_mode": options.permission_mode,
        "session_id": options.session_id,
        "resume": options.resume,
        "fork_session": options.fork_session,
        "name": (options.extra_args or {}).get("name"),
        "settings": options.settings,
        "setting_sources": list(options.setting_sources)
        if options.setting_sources is not None
        else "default",
        "add_dirs": [str(p) for p in options.add_dirs],
        "mcp_servers": sorted(options.mcp_servers)
        if isinstance(options.mcp_servers, dict)
        else str(options.mcp_servers),
        **_mcp_preview(options),
        "allowed_tools": len(options.allowed_tools),
        "disallowed_tools": len(options.disallowed_tools),
        "env": sorted(_effective_child_environment(options)),
        "json_schema": sorted((options.output_format or {}).get("schema", {}).get("properties", {})),
    }


def _effective_child_environment(options: ClaudeAgentOptions) -> dict[str, str]:
    from .isolated_transport import child_environment

    return child_environment(options)


WORKER_ENV_BASE = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TERM",
    "LANG",
    "LC_*",
    "TZ",
    "SSH_AUTH_SOCK",
)


def worker_env(root: Path, d: dict[str, Any], repo: repos_mod.Repo | None = None) -> dict[str, str]:
    patterns = [*with_platform_names(WORKER_ENV_BASE), *(repo.env_allow if repo else [])]
    env = {k: v for k, v in os.environ.items() if not claude_named(k) and env_allowed(k, patterns)}
    for key, value in as_dict(repo.dispatch.get("env") if repo else None).items():
        env[str(key)] = str(value)
    env["AGENT_ROOT"] = str(root)
    env["AGENT_DISPATCH_ID"] = d["id"]
    env["AGENT_FEATURE"] = f"{int(d['feature']):03d}"
    return strip_routing(env)


VERIFIER_ENV_BASE = tuple((name for name in WORKER_ENV_BASE if name != "SSH_AUTH_SOCK"))
VERIFIER_LOCALE = "C.UTF-8"


def verifier_env(repo: repos_mod.Repo) -> dict[str, str]:
    patterns = [*with_platform_names(VERIFIER_ENV_BASE), *repo.env_allow]
    env = {
        k: v
        for k, v in os.environ.items()
        if not claude_named(k) and (not k.startswith("LC_")) and (k != "LANG") and env_allowed(k, patterns)
    }
    env |= {"LANG": VERIFIER_LOCALE, "LC_ALL": VERIFIER_LOCALE}
    for key, value in as_dict(repo.dispatch.get("env")).items():
        env[str(key)] = str(value)
    return strip_routing(env)


def next_rubric_tier(
    model: str | None, effort: str | None, provider: str = rubric_mod.CLAUDE
) -> tuple[str | None, str | None]:
    ladder = rubric_mod.MODEL_LADDERS.get(provider, ())
    low = (model or "").lower()
    if provider == rubric_mod.CODEX and low == "gpt-6-sol":
        low = "gpt-6.1-sol"
    for i, rung in enumerate(ladder):
        if rung in low and i + 1 < len(ladder):
            return (ladder[i + 1], effort)
    return (model, effort)
