"""Assemble worker files, launch and resume options, and rubric escalation tiers.

Function-local imports from `rules.py` defer permission construction and supervisor callbacks. The
rules facade re-exports these option builders without resolving attributes on a partially imported
module.
"""

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
from ..launcher import is_windows
from ..records.narrow import as_dict, as_int, dig
from . import mcp_policy
from . import rubric as rubric_mod
from .env_names import claude_named, env_allowed, with_platform_names
from .return_schema import RETURN_SCHEMA
from .state import WORKER_CONTRACT, file
from .telemetry_events import strip_routing

# The SDK reads the worker CLI's stdout one JSON message at a time and dies on a message larger
# than its buffer, 1 MB by default. One tool result is one message, so a worker that reads a big
# file or polls a busy delegate can end its own session: a
# TaskOutput of 1.2 MB once lost an attempt that way. Sixteen times the default leaves room for any single
# result a worker should produce.
WORKER_MAX_BUFFER_BYTES = 16 * 1024 * 1024


@dataclass
class _WorkerOptions(ClaudeAgentOptions):
    """SDK options plus redacted provenance for previews; the SDK ignores this metadata."""

    mcp_selection: dict[str, Any] = field(default_factory=dict[str, Any], repr=False)


def write_worker_files(
    root: Path, d: dict[str, Any], repo: repos_mod.Repo, settings: dict[str, Any] | None = None
) -> mcp_policy.ResolvedMcp:
    """The three files a worker launches against. `settings` is the already-built worker
    settings when the caller has them. Omit it and they are built here. This is
    what the supervisor does: it regenerates them per attempt on purpose. Return the resolved
    MCP composition for the same attempt's build_options call; never cache it across attempts."""
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
    """The `claude` the worker runs as. Never the CLI bundled inside claude-agent-sdk: a
    worker has to be the same binary an interactive session is, so the owner can
    `claude --resume` into it and see the same behaviour (see ADR-0009). A missing one fails loudly
    here rather
    than letting the SDK silently fall back to its own copy."""
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
    """Everything the worker runs with (A3), as SDK options instead of a claude argv.

    `resume` swaps the pinned `session_id` for `resume`, which continues the SAME session
    rather than relaunching it: the follow-up composed by `compose_followup` is simply the
    next query on that session.

    Two shapes learned in the spike and worth not rediscovering: `allowed_tools` and
    `disallowed_tools` take the permission-rule strings `build_settings()` already produces
    (`Bash(git commit:*)`, not `Bash`). `mcp_servers` wants the inner mapping, not
    `build_mcp_config`'s `{"mcpServers": ...}` wrapper. `setting_sources` is left unset so
    the worktree's own CLAUDE.md, AGENTS.md and skills load exactly as they would for a
    human in that directory, except in a dispatch into this harness itself, where the
    worker loads the user source only (`setting_sources_for`).

    `settings` is the already-built worker settings when the caller has them.
    Omit it and they are built here. `mcp` is the composition returned by write_worker_files
    for this attempt; standalone previews resolve once here without writing sidecars.
    """
    # The supervisor owns the `can_use_tool` closure, because answering is also recording
    # and the record is its file. Imported here rather than at module scope: `supervisor`
    # imports this module by name at ITS module scope, so the other direction has to be lazy.
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
        # Every tool call the rules above neither allowed nor denied reaches the supervisor
        # while the worker is still inside the call: recorded first-hand with a reason, then
        # denied. Persist the reason while the call is blocked, independently of final denials.
        "can_use_tool": permission_callback(root, d, repo),
        "output_format": {"type": "json_schema", "schema": RETURN_SCHEMA},
        "env": worker_env(root, d, repo),
        # the .settings.json the harness writes, so the permission rules, the PreCompact hook
        # and crossSessionInbound reach the worker the same way they did under `--settings`
        "settings": str(file(root, d["id"], ".settings.json")),
        "add_dirs": [str(root / d["feature_dir"])],
        "max_budget_usd": float(d["budget_usd"]),
        "max_buffer_size": WORKER_MAX_BUFFER_BYTES,
        # The address a human or another session sends to. A worker binds an inbox socket and
        # reads peer messages between tool calls, so `@<dispatch-id>` from the owner's own
        # terminal reaches it directly instead of being relayed through the manager. The dispatch
        # id is the name because it is what they already see in every
        # status line.
        "extra_args": {"name": d["id"], **extra_args},
    }
    # Bound tool loops independently of budget and wall time. Store the cap on the dispatch so
    # escalation preserves its launch limit.
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
            # `--escalate-from`: the escalated worker resumes the OLD session and
            # forks it, carrying the previous attempt's real context (everything it read, ran
            # and was told) instead of a pasted copy of the last manager message.
            # The pinned `session_id` is kept alongside: the CLI allows --session-id with
            # --resume precisely when --fork-session is set, so the fork lands on the id this
            # record already committed to. `evaluate`'s session cross-check still holds.
            kwargs["resume"] = str(forked)
            kwargs["fork_session"] = True
    if d.get("model"):
        kwargs["model"] = str(d["model"])
    if d.get("effort"):
        # Carry explicit model and effort choices on the dispatch so resumes preserve them.
        kwargs["effort"] = str(d["effort"])
    return _WorkerOptions(**kwargs, mcp_selection=mcp.preview())


def _harness_self(root: Path, repo: repos_mod.Repo) -> bool:
    """True when the registered repo is this harness: its clone (the main checkout or a worktree
    of it) shares this root's repository. False on any error, which keeps the default sources."""
    from ..worktree import main_checkout

    try:
        return main_checkout(repo.abs_path(root)).resolve() == main_checkout(root).resolve()
    except AgentError, ExternalFailure, OSError:
        return False


def setting_sources_for(root: Path, repo: repos_mod.Repo) -> list[str] | None:
    """Load user/project/local settings for product repos, but only user settings for this harness.
    Harness project and local sources belong to the manager and could run manager hooks or render
    shared notes.

    Workers retain explicit --settings policy, PreCompact, SDK hooks, appended worker contract and
    work order. Those are not setting sources. Resume uses the same source selection.
    """
    return ["user"] if _harness_self(root, repo) else None


def _repo_extra_args(repo: repos_mod.Repo) -> dict[str, str | None]:
    """Compile validated raw flags; no value or unknown malformed text reaches errors."""
    try:
        return parse_extra_args(repo.dispatch.get("extra_args"))
    except ValueError as exc:
        raise AgentError(str(exc)) from None


def _repo_mcp_file(cwd: str | Path | None, clone: str | Path | None) -> dict[str, Any] | None:
    """The `mcpServers` mapping of the worktree's own `.mcp.json` (the clone's while the
    worktree does not exist yet, which is every `--dry-run`). `{}` when there is no file,
    `None` when it cannot be read."""
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
    """Legacy best-effort inventory, not used by worker launch or preview composition."""
    servers = _repo_mcp_file(cwd, clone)
    return ["(unreadable .mcp.json)"] if servers is None else sorted(servers)


def _mcp_preview(options: ClaudeAgentOptions) -> dict[str, Any]:
    if not isinstance(options, _WorkerOptions):
        return {"strict_mcp_config": options.strict_mcp_config}
    sources = options.mcp_selection["servers"]
    return {
        "strict_mcp_config": options.strict_mcp_config,
        "mcp_servers": sorted(source["server"] for source in sources),
        "mcp_selection": options.mcp_selection,
        "repo mcp servers": sorted(
            source["server"]
            for source in sources
            if source["definition_source"].startswith(("worktree:", "clone:"))
        ),
        "repo mcp policy": "explicit selection only; unselected sources not inventoried",
    }


CODEX_TURN_NOTE = (
    "not applicable to codex: Claude counts conversation turns; Codex is capped by max_tool_items"
)
# Codex has no conversation-turn cap, so cap completed tool items instead. Measured streams use
# 1–163 items (builds 81–163, reviews up to 84); 400 permits larger builds while bounding loops.
DEFAULT_MAX_TOOL_ITEMS = 400


def max_tool_items(d: dict[str, Any], repo: repos_mod.Repo) -> int:
    """The dispatch's Codex tool-item cap: its own, else the repo's `dispatch.max_tool_items`."""
    return int(d.get("max_tool_items") or as_int(repo.dispatch.get("max_tool_items"), DEFAULT_MAX_TOOL_ITEMS))


def launch_options_preview(
    root: Path, d: dict[str, Any], repo: repos_mod.Repo, *, settings: Any
) -> dict[str, Any]:
    """The dry run's options block, for the provider that will actually run the worker."""
    if d.get("provider") == "codex":
        return codex_options_preview(d, repo, resume=False, root=root)
    return options_preview(build_options(root, d, repo, resume=False, settings=settings), repo.abs_path(root))


def codex_options_preview(
    d: dict[str, Any], repo: repos_mod.Repo, *, resume: bool, root: Path | None = None
) -> dict[str, Any]:
    """The Codex counterpart of `options_preview`, for the dry run and the record. Codex has no
    conversation-turn cap, so `max_turns` is shown as not applicable beside the tool-item cap
    that does apply; message-only progress is bounded by the timeout and the budget alone."""
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


# Keys worth showing a human in a `--dry-run` preview and keeping on the record. The full
# options carry the worker's entire environment and a 75-rule allow list, which is noise in
# both places.
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
        # Provenance belongs to this composition; no filesystem inventory or second read.
        **_mcp_preview(options),
        "allowed_tools": len(options.allowed_tools),
        "disallowed_tools": len(options.disallowed_tools),
        # Show allowed environment names so the owner can review the grant; values could expose
        # credentials in the preview and record.
        "env": sorted(_effective_child_environment(options)),
        "json_schema": sorted((options.output_format or {}).get("schema", {}).get("properties", {})),
    }


def _effective_child_environment(options: ClaudeAgentOptions) -> dict[str, str]:
    from .isolated_transport import child_environment

    return child_environment(options)


# Inherit only explicitly allowed environment variables. Workers read untrusted content and can use
# WebFetch, so inherited credentials create an injection-to-exfiltration path. Keep the public base
# minimal; Windows adds WINDOWS_ENV_BASE. Repos declare toolchain names in dispatch.env_allow.
# ANTHROPIC_BASE_URL can route source and traffic to another host, so it requires an explicit
# dispatch.env entry.
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
    "SSH_AUTH_SOCK",  # the clones use SSH remotes and `Bash(git fetch:*)` is allowed
)


def worker_env(root: Path, d: dict[str, Any], repo: repos_mod.Repo | None = None) -> dict[str, str]:
    """The worker's environment: `WORKER_ENV_BASE` plus the repo's `dispatch.env_allow`, taken
    from `os.environ`, plus what our hooks and MCP server need, plus the repo's `dispatch.env`.

    Nothing else is inherited. Inherited `CLAUDE*` variables are excluded even when a repo
    allow pattern matches. Explicit manager identity variables are rejected by the launch
    adapter, which adds its own SDK tags. Canonical AGENT ownership tags win over repo values.

    `dispatch.env` exists because a worker cannot set one itself. Its allow-list is a prefix
    match, so `env FOO=1 pnpm install` starts with `env` and matches no `Bash(pnpm:*)` rule.
    A worker can burn turns discovering that while trying to get past a broken install step.
    Declaring the variable here beats widening the allow-list to `Bash(env:*)`,
    which would let a worker prefix any command at all. `dispatch.env` sets a value;
    `dispatch.env_allow` only lets an existing one through.

    The telemetry routing keys never reach a worker, whatever supplied them: only the verifier
    the supervisor launches records.
    """
    patterns = [*with_platform_names(WORKER_ENV_BASE), *(repo.env_allow if repo else [])]
    env = {k: v for k, v in os.environ.items() if not claude_named(k) and env_allowed(k, patterns)}
    for key, value in as_dict(repo.dispatch.get("env") if repo else None).items():
        env[str(key)] = str(value)
    # Repository settings cannot redirect ownership, hooks, or the manager's feature routing.
    env["AGENT_ROOT"] = str(root)
    env["AGENT_DISPATCH_ID"] = d["id"]
    env["AGENT_FEATURE"] = f"{int(d['feature']):03d}"
    return strip_routing(env)


# `WORKER_ENV_BASE` minus `SSH_AUTH_SOCK`: a supervisor verification run executes the repo's
# own build/test commands inside an already-checked-out worktree (see ADR-0018) and has no
# reason to reach a remote, unlike a worker that fetches and pushes. `verification_process.BASE_ENV`
# (`{"PATH": os.defpath, ...}`) once made every registered repo's supervisor check fail:
# `os.defpath` is POSIX's `/usr/bin:/bin:/usr/sbin:/sbin`, which has no Homebrew tools on it. A repo whose checks genuinely need SSH names it in `dispatch.env_allow`, same as any
# other grant, where `verifier_env`'s own dry-run line (see `verification_preview_lines`)
# prints it before launch.
VERIFIER_ENV_BASE = tuple(name for name in WORKER_ENV_BASE if name != "SSH_AUTH_SOCK")
#: The verifier's locale, the same on every machine and shell (`verifier_env`).
VERIFIER_LOCALE = "C.UTF-8"


def verifier_env(repo: repos_mod.Repo) -> dict[str, str]:
    """Build the verifier environment from `VERIFIER_ENV_BASE`, repo `dispatch.env_allow` and
    `dispatch.env`.

    Exclude `SSH_AUTH_SOCK` and worker ownership tags: `verification.prepare` stamps verifier
    identity. Pin the locale to `VERIFIER_LOCALE` so equivalent shells share a certificate key;
    explicit repo environment values still override it.
    """
    patterns = [*with_platform_names(VERIFIER_ENV_BASE), *repo.env_allow]
    env = {
        k: v
        for k, v in os.environ.items()
        if not claude_named(k) and not k.startswith("LC_") and k != "LANG" and env_allowed(k, patterns)
    }
    env |= {"LANG": VERIFIER_LOCALE, "LC_ALL": VERIFIER_LOCALE}
    # Windows pytest/getpass and git fixtures need the same identity as the launching shell.
    # HOME is often absent in a native shell even when USERPROFILE is available.
    if is_windows():
        for name in ("HOME", "USERNAME", "USERPROFILE"):
            if value := os.environ.get(name):
                env[name] = value
        if not env.get("HOME") and env.get("USERPROFILE"):
            env["HOME"] = env["USERPROFILE"]
    for key, value in as_dict(repo.dispatch.get("env")).items():
        env[str(key)] = str(value)
    return strip_routing(env)


def next_rubric_tier(
    model: str | None,
    effort: str | None,
    provider: str = rubric_mod.CLAUDE,
) -> tuple[str | None, str | None]:
    """Step up one model rung while keeping effort, including omission, unchanged."""
    ladder = rubric_mod.MODEL_LADDERS.get(provider, ())
    low = (model or "").lower()
    # Historical Sol records still occupy the middle rung after the active target moves.
    if provider == rubric_mod.CODEX and low == "gpt-6-sol":
        low = "gpt-6.1-sol"
    for i, rung in enumerate(ladder):
        if rung in low and i + 1 < len(ladder):
            return ladder[i + 1], effort
    return model, effort
