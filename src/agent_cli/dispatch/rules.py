"""Public facade for worker permission settings, launch options, command preflight and runtime
callbacks. Deferred imports preserve call-time lookup without loading provider SDKs on hook paths.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import stat
from pathlib import Path, PurePath
from typing import Any, Literal, cast

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
    # defined here
    "DENY_RULE_REASON",
    "GIT_READ_REASON",
    "NO_RULE_REASON",
    "build_mcp_config",
    "decide",
    "dispatch_rules",
    "sdk_hooks",
    "shown_deny",
    # options.py
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
    # preflight.py
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
    # settings.py
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
    """Stop and SessionEnd as in-process callbacks on the supervisor's own client.

    Same behaviour the `worker-stop` command hook had: record the worker's transcript path on
    the dispatch and route the capture into the feature's .agent/raw/. It runs in the process that
    is
    already there instead of re-execing `bin/agent` (a python start plus the CLI import,
    measured at roughly 400 ms) at the end of every assistant turn. The `hook worker-stop`
    entrypoint stays: both call `supervisor.worker_stop`, so there is one implementation and
    the CLI form still works for anything outside an SDK session.

    A callback never raises into the worker. Same contract as every hook in `hooks/`: a
    failure here must not wedge or end a turn that is otherwise fine.

    The keys are plain `str`, not `claude_agent_sdk.types.HookEvent`. That Literal is missing
    `SessionEnd` in 0.2.152, even though the CLI's own hook-event enum has it. The SDK only
    ever forwards the mapping's keys verbatim in the `initialize` request. Typing this
    `HookEvent` would mean either dropping SessionEnd or casting a lie.
    """

    async def _stop(payload: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        from .supervisor import worker_stop

        try:
            worker_stop(root, dict(payload or {}))
        except Exception:  # noqa: BLE001, S110 — a hook never raises; failing open is the contract
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
        except Exception:  # noqa: BLE001 — a broken security hook must refuse the tool
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


# The can_use_tool callback handles only calls matching neither allow nor deny rules. Whole-tool
# grants approve before the callback; rules remain primary and the callback records and denies
# fall-throughs. Do not hold these calls awaiting unconfigured owner approvals.

# Name the rule family in each refusal so the worker distinguishes forbidden operations from
# missing coverage and can stop rather than retry spellings.
DENY_RULE_REASON = "{rule} is never a worker's. Return `question` naming the command and stop."
NO_RULE_REASON = (
    "no rule covers this command; return `question` with it verbatim. Do not try another "
    "spelling: the fix is a rule in the harness, not a workaround here."
)
GIT_READ_REASON = "read-only git verb `{verb}` against {what}"


def decide(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    d: dict[str, Any],
    repo: repos_mod.Repo,
    root: Path,
) -> tuple[Literal["allow", "deny"], str]:
    """`(behavior, reason)` for one tool call that fell through the permission rules.

    Pure: it reads the registry and the dispatch record and writes nothing. The supervisor can
    record the answer before handing it back. The whole thing is testable without a worker.

    In order. The order is the invariant: nothing here may allow what a deny rule denies.

    1. a `BASE_DENY`, repo or selected `--mcp` profile deny match, scored by
       `permissions.matches` (which splits a compound command and lets deny win on any single
       part);
    2. a chained Bash command, by the manager's own `hooks.is_chained`, denied with the same
       respelling hint the manager's hook gives. The no-chaining rule was hook-enforced for
       the manager and only advisory for workers until this. a worker's chained command was
       allowed;
    3. `git -C <path> <read verb>` where `<path>` is a registered clone or this dispatch's own
       worktree: allowed. This is the one case rule strings could not reach without generating
       one rule per clone per verb (128 of them today, still in `build_settings`). Removing
       them is a follow-up once a live run shows the callback catching these);
    4. a command in the read-only class (`readonly_policy.py`): allowed, on both providers;
    5. a native `Edit` of this worktree's own regular `.claude/settings.json`, when the dispatch's
       `writes` names that exact file, the repo grants exactly `Edit(//<that absolute path>)` and
       no deny rule could cover it (`_settings_edit`): allowed;
    6. everything else: denied, naming the command as the thing to ask about.

    `repo` is here because the deny list is built from the worker's own settings, and a repo
    that ever adds deny rules of its own should be read the same way BASE_DENY is.
    """
    if tool_name == "Edit":
        return _settings_edit(tool_input, d, repo, root)
    command = _bash_command(tool_input) if tool_name == "Bash" else ""
    if command:
        rules, hide = dispatch_rules(root, repo, d)
        verdict, rule = permissions.matches(command, [], rules["deny"])
        if verdict == "denied":
            return "deny", DENY_RULE_REASON.format(rule=f"`{shown_deny(str(rule), hide)}`")
        # imported here, not at module scope: `hooks` pulls in capture/sessions/status for two
        # functions. This module is imported on every `build_settings` call, which is every
        # preview and every launch. This path runs only on a fall-through.
        from .. import hooks as hooks_mod

        if hooks_mod.is_chained(command):
            return "deny", permissions.shown_text(
                hooks_mod.NO_CHAIN_REASON + hooks_mod.respelling_hint(command)
            )
        allowed = _git_read_target(command, d, root) or readonly_policy.reason(command)
        if allowed:
            return "allow", allowed
    return "deny", NO_RULE_REASON


def dispatch_rules(
    root: Path, repo: repos_mod.Repo, d: dict[str, Any]
) -> tuple[dict[str, list[str]], list[str]]:
    """The worker's allow and deny rules with the profiles its record names in `mcp`, and the
    selected server names a reason must not print (`shown_deny`). A malformed, unknown or unready
    selection raises without the profile's paths or hints, so the caller refuses the tool."""
    try:
        names = mcp_policy._names(d.get("mcp"), "dispatch.mcp")  # pyright: ignore[reportPrivateUsage]
        profiles = worker_mcp.require(root, names)
    except AgentError, OSError, ValueError, yaml.YAMLError, RecursionError:
        raise AgentError(
            "selected worker MCP profiles are unknown, invalid, colliding, or not ready"
        ) from None
    rules = build_settings(root, repo, worktree=d.get("worktree"), mcp=names)["permissions"]
    return rules, selected_servers(repo, profiles)


def shown_deny(rule: str, hide: list[str]) -> str:
    """`rule` as a deny reason names it, or `permissions.shown_rule`'s marker when it holds a
    selected name that may not be printed."""
    shown = permissions.shown_rule(rule, hide)
    return rule if shown == repr(rule) else shown


def _bash_command(tool_input: dict[str, Any] | None) -> str:
    return str((tool_input or {}).get("command") or "").strip() if isinstance(tool_input, dict) else ""


def _git_read_target(command: str, d: dict[str, Any], root: Path) -> str:
    """The reason string for a `git -C <path> <verb>` this worker may run against a repo it was
    not dispatched to, or `""` when the command is not that shape. Reads only: no ref movement,
    no index, no network. Same `GIT_READ_VERBS` subset `build_settings` grants per clone.
    The path has to be the clone or the worktree exactly, as the generated rules require. A
    subdirectory is not a registered repo."""
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


SETTINGS_FILE = ".claude/settings.json"
SETTINGS_EDIT_REASON = "the declared `.claude/settings.json` edit, granted exactly by the repo"


def _settings_edit(
    tool_input: dict[str, Any] | None, d: dict[str, Any], repo: repos_mod.Repo, root: Path
) -> tuple[Literal["allow", "deny"], str]:
    """Allow one native Edit of the worktree's own settings file when both the dispatch's writes and
    the repo's exact grant consent to it. Anything not provably that file is a plain fall-through."""
    target = _settings_target(tool_input, d)
    if target is None or _settings_grant(target) not in repo.allowed_tools:
        return "deny", NO_RULE_REASON
    try:
        rules, hide = dispatch_rules(root, repo, d)
    except AgentError:
        return "deny", NO_RULE_REASON
    for rule in rules["deny"]:
        if _edit_deny_covers(rule, target):
            return "deny", DENY_RULE_REASON.format(rule=f"`{shown_deny(rule, hide)}`")
    return "allow", SETTINGS_EDIT_REASON


def _settings_target(tool_input: dict[str, Any] | None, d: dict[str, Any]) -> Path | None:
    """The worktree's `.claude/settings.json` when `tool_input` is a well-formed Edit of exactly that
    regular file, reached through no symlink, and `writes` names it; otherwise None."""
    from .write_scope import canonical_writes

    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or "\0" in raw or _dotted(raw):
        return None
    if not all(isinstance(tool_input.get(key), str) for key in ("old_string", "new_string")):
        return None
    if not isinstance(tool_input.get("replace_all", False), bool):
        return None
    writes, worktree = d.get("writes"), d.get("worktree")
    if not isinstance(writes, list):
        return None
    items = cast("list[object]", writes)
    paths = [w for w in items if isinstance(w, str)]
    if len(paths) != len(items):
        return None
    try:
        if SETTINGS_FILE not in (canonical_writes(paths) or []):
            return None
    except AgentError:
        return None
    if not isinstance(worktree, str) or not worktree or _dotted(worktree):
        return None
    wt = Path(worktree)
    target = wt / ".claude" / "settings.json"
    path = Path(raw)
    if not wt.is_absolute() or not path.is_absolute() or path != target:
        return None
    try:
        if target.resolve(strict=True) != target or not stat.S_ISREG(os.lstat(target).st_mode):
            return None
    except OSError, RuntimeError:
        return None
    return target


def _dotted(path: str) -> bool:
    return any(part in (".", "..") for part in re.split(r"[\\/]", path))


def _settings_grant(target: PurePath) -> str:
    """The one repo rule that grants `target`: Claude Code's `//` absolute-path spelling."""
    return f"Edit(//{target.as_posix().lstrip('/')})"


def _edit_deny_covers(rule: str, target: PurePath) -> bool:
    """Whether deny `rule` could cover an Edit of `target`. Only an absolute exact path or
    `<dir>/**` is read as a path; any other Edit rule shape counts as covering it."""
    match = permissions.RULE_RE.match(rule.strip())
    if match is None:
        return True
    if not fnmatch.fnmatchcase("Edit", match.group("tool")):
        return False
    arg = match.group("arg")
    if arg is None or not arg.startswith("//"):
        return True
    body = arg[2:].lstrip("/").replace("\\", "/")
    tree = body.endswith("/**")
    base = body[:-3] if tree else body
    if not base or any(ch in base for ch in "*?[{") or "//" in base or _dotted(base):
        return True
    have, want = target.as_posix().lstrip("/").casefold(), base.rstrip("/").casefold()
    return have == want or (tree and have.startswith(want + "/"))


def build_mcp_config(
    root: Path,
    d: dict[str, Any],
    repo: repos_mod.Repo | None = None,
    *,
    mcp: mcp_policy.ResolvedMcp | None = None,
) -> dict[str, Any]:
    """The explicit composition, reused by sidecar and SDK callers within one attempt."""
    if mcp is None:
        if repo is None:
            repo = repos_mod.load_registry(root).require(str(d["repo"]))
        mcp = mcp_policy.resolve(root, repo, d)
    return mcp.config()
