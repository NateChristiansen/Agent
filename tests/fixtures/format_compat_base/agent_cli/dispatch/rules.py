# ruff: noqa
from __future__ import annotations
import os
import shlex
from pathlib import Path
from typing import Any, Literal
import yaml
from claude_agent_sdk import HookContext, HookInput, HookJSONOutput, HookMatcher
from .. import permissions, worker_mcp
from .. import repos as repos_mod
from ..core import AgentError
from . import mcp_policy, readonly_policy
from .options import (
    RETURN_SCHEMA,
    build_options,
    claude_on_path,
    codex_options_preview,
    launch_options_preview,
    next_rubric_tier,
    options_preview,
    repo_mcp_servers,
    verifier_env,
    worker_env,
    write_worker_files,
)
from .preflight import (
    BUILD_ROW,
    KNOWN_BINARIES,
    PER_COMMIT_RE,
    PRECEDENTS_WARNING,
    SHELL_FENCE_TAGS,
    UNVERIFIABLE_CLAIM,
    check_preflight,
    extract_candidate_commands,
    per_commit_check,
    precedents_check,
    preflight,
)
from .settings import (
    BASE_ALLOW,
    BASE_DENY,
    GIT_READ_VERBS,
    GIT_VERBS,
    build_settings,
    registry_repos,
    selected_servers,
)

__all__ = [
    "DENY_RULE_REASON",
    "GIT_READ_REASON",
    "NO_RULE_REASON",
    "build_mcp_config",
    "decide",
    "dispatch_rules",
    "sdk_hooks",
    "shown_deny",
    "RETURN_SCHEMA",
    "build_options",
    "claude_on_path",
    "next_rubric_tier",
    "codex_options_preview",
    "launch_options_preview",
    "options_preview",
    "repo_mcp_servers",
    "verifier_env",
    "worker_env",
    "write_worker_files",
    "BUILD_ROW",
    "KNOWN_BINARIES",
    "PER_COMMIT_RE",
    "PRECEDENTS_WARNING",
    "SHELL_FENCE_TAGS",
    "UNVERIFIABLE_CLAIM",
    "check_preflight",
    "extract_candidate_commands",
    "per_commit_check",
    "precedents_check",
    "preflight",
    "BASE_ALLOW",
    "BASE_DENY",
    "GIT_READ_VERBS",
    "GIT_VERBS",
    "build_settings",
    "registry_repos",
]


def sdk_hooks(
    root: Path, worktree: Path | None = None, start_sha: str | None = None, dispatch_id: str | None = None
) -> dict[str, list[HookMatcher]]:

    async def _stop(payload: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        from .supervisor import worker_stop

        try:
            worker_stop(root, dict(payload or {}))
        except Exception:
            pass
        return {}

    async def _redirect(payload: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        from ..hooks.worker_comments import comment_noise
        from ..hooks.worker_push import git_push
        from ..hooks.worker_records import record_reach
        from ..hooks.worker_redirect import outside_redirect
        from ..hooks.worker_tests import full_suite

        if payload.get("tool_name") != "Bash" or worktree is None:
            return {}
        command = ""
        try:
            tool_input = payload.get("tool_input") or {}
            command = str(tool_input.get("command") or "")
            reason = (
                outside_redirect(command, worktree)
                or git_push(command, worktree)
                or full_suite(command)
                or record_reach(command)
                or comment_noise(command, worktree, start_sha)
            )
        except Exception:
            reason = "Worker redirect policy could not inspect this command"
        if reason is None:
            return {}
        if dispatch_id:
            from ..hooks.decision_log import worker_denial

            worker_denial(root, dispatch_id, "claude", "worker-policy", command, reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": permissions.shown_text(reason),
            }
        }

    matcher = HookMatcher(hooks=[_stop])
    hooks = {"Stop": [matcher], "SessionEnd": [matcher]}
    if worktree is not None:
        hooks["PreToolUse"] = [HookMatcher(matcher="Bash", hooks=[_redirect])]
    return hooks


DENY_RULE_REASON = "{rule} is never a worker's. Return `question` naming the command and stop."
NO_RULE_REASON = "no rule covers this command; return `question` with it verbatim. Do not try another spelling: the fix is a rule in the harness, not a workaround here."
GIT_READ_REASON = "read-only git verb `{verb}` against {what}"


def decide(
    tool_name: str, tool_input: dict[str, Any] | None, d: dict[str, Any], repo: repos_mod.Repo, root: Path
) -> tuple[Literal["allow", "deny"], str]:
    command = _bash_command(tool_input) if tool_name == "Bash" else ""
    if command:
        rules, hide = dispatch_rules(root, repo, d)
        verdict, rule = permissions.matches(command, [], rules["deny"])
        if verdict == "denied":
            return ("deny", DENY_RULE_REASON.format(rule=f"`{shown_deny(str(rule), hide)}`"))
        from .. import hooks as hooks_mod

        if hooks_mod.is_chained(command):
            return (
                "deny",
                permissions.shown_text(hooks_mod.NO_CHAIN_REASON + hooks_mod.respelling_hint(command)),
            )
        allowed = _git_read_target(command, d, root) or readonly_policy.reason(command)
        if allowed:
            return ("allow", allowed)
    return ("deny", NO_RULE_REASON)


def dispatch_rules(
    root: Path, repo: repos_mod.Repo, d: dict[str, Any]
) -> tuple[dict[str, list[str]], list[str]]:
    try:
        names = mcp_policy._names(d.get("mcp"), "dispatch.mcp")
        profiles = worker_mcp.require(root, names)
    except AgentError, OSError, ValueError, yaml.YAMLError, RecursionError:
        raise AgentError(
            "selected worker MCP profiles are unknown, invalid, colliding, or not ready"
        ) from None
    rules = build_settings(root, repo, worktree=d.get("worktree"), mcp=names)["permissions"]
    return (rules, selected_servers(repo, profiles))


def shown_deny(rule: str, hide: list[str]) -> str:
    shown = permissions.shown_rule(rule, hide)
    return rule if shown == repr(rule) else shown


def _bash_command(tool_input: dict[str, Any] | None) -> str:
    return str((tool_input or {}).get("command") or "").strip() if isinstance(tool_input, dict) else ""


def _git_read_target(command: str, d: dict[str, Any], root: Path) -> str:
    try:
        toks = shlex.split(command)
    except ValueError:
        return ""
    if len(toks) < 4 or os.path.basename(toks[0]) != "git" or toks[1] != "-C":
        return ""
    if toks[3] not in GIT_READ_VERBS:
        return ""
    target = os.path.normpath(os.path.expanduser(toks[2]))
    worktree = d.get("worktree")
    if worktree and target == os.path.normpath(str(worktree)):
        return GIT_READ_REASON.format(verb=toks[3], what="this dispatch's own worktree")
    for other in registry_repos(root):
        clone = other.abs_path(root)
        if target == os.path.normpath(str(clone)):
            return GIT_READ_REASON.format(verb=toks[3], what=f"the registered clone `{other.short}`")
    return ""


def build_mcp_config(
    root: Path,
    d: dict[str, Any],
    repo: repos_mod.Repo | None = None,
    *,
    mcp: mcp_policy.ResolvedMcp | None = None,
) -> dict[str, Any]:
    if mcp is None:
        if repo is None:
            repo = repos_mod.load_registry(root).require(str(d["repo"]))
        mcp = mcp_policy.resolve(root, repo, d)
    return mcp.config()
