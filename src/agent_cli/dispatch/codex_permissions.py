"""Codex PreToolUse policy for a dispatched worker.

The hook runs in the manager's Python environment, outside the worker sandbox. It
uses the same repository allow and deny rules as the Claude worker.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

from .. import permissions, repos
from ..core import AgentError, iso, now
from ..records.narrow import as_dict, as_dict_or_none
from . import powershell_spelling, state
from .rules import NO_RULE_REASON, decide, dispatch_rules, shown_deny

_REDACTED_IDENTITY = "[redacted]"


def _redacted_identities(event: dict[str, Any]) -> dict[str, str]:
    """Do not record native identity values before their format is validated."""
    return {
        field: _REDACTED_IDENTITY for field in ("tool_use_id", "turn_id") if isinstance(event.get(field), str)
    }


def hook_config() -> dict[str, Any]:
    # `-I`: the hook imports the harness this interpreter was installed with, never what the
    # worker's PYTHONPATH or working directory would put first (a candidate or product checkout).
    command = f"{sys.executable} -I -m agent_cli.dispatch.codex_permissions"
    return {
        "PreToolUse": [
            {
                "matcher": "Bash|mcp__.*",
                "hooks": [{"type": "command", "command": command, "timeout": 15}],
            }
        ]
    }


def decision(root: Path, dispatch_id: str, event: dict[str, Any]) -> tuple[str, str]:
    """`(allow|deny|ask, reason)` for a Codex worker's tool call; every deny is logged with its
    dispatch id (worker denials were invisible in telemetry)."""
    verdict, reason = _decision(root, dispatch_id, event)
    if verdict == "deny":
        from ..hooks.decision_log import worker_denial

        tool = str(event.get("tool_name") or "")
        # An MCP tool's own `command` argument is not a shell command to summarize.
        command = str(as_dict(event.get("tool_input")).get("command") or "") if tool == "Bash" else ""
        worker_denial(
            root, dispatch_id, "codex", "worker-policy", command or permissions.shown_tool(tool), reason
        )
    return verdict, reason


def _decision(root: Path, dispatch_id: str, event: dict[str, Any]) -> tuple[str, str]:
    d = state.load(root, dispatch_id)
    repo = repos.load_registry(root).require(str(d["repo"]))
    tool = str(event.get("tool_name") or "")
    tool_input = as_dict(event.get("tool_input"))
    rules, hide = dispatch_rules(root, repo, d)
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        from ..hooks.worker_comments import comment_noise
        from ..hooks.worker_push import git_push
        from ..hooks.worker_records import record_reach
        from ..hooks.worker_redirect import outside_redirect
        from ..hooks.worker_tests import full_suite

        worktree = Path(str(d["worktree"]))
        # On Windows the body is PowerShell; its POSIX twin is judged too, and a deny of either
        # spelling wins (`powershell_spelling`).
        twin = powershell_spelling.respelled(command)
        spellings = [command] if twin is None else [command, twin]
        for spelled in spellings:
            if reason := (
                outside_redirect(spelled, worktree)
                or git_push(spelled, worktree)
                or full_suite(spelled)
                or record_reach(spelled)
            ):
                return "deny", reason
        try:
            reason = comment_noise(command, worktree, d.get("start_sha"))
        except Exception as exc:  # noqa: BLE001 — a broken check must refuse the commit
            reason = f"Worker comment check failed: {type(exc).__name__}"
        if reason:
            return "deny", reason
        verdicts = [permissions.matches(spelled, rules["allow"], rules["deny"]) for spelled in spellings]
        for verdict, rule in verdicts:
            if verdict == "denied":
                return "deny", f"Denied by dispatch rule {shown_deny(str(rule), hide)}"
        # No chaining even where a broad allow pattern matches the first segment.
        from .. import hooks

        if hooks.is_chained(command):
            return "deny", hooks.NO_CHAIN_REASON
        for verdict, rule in verdicts:
            if verdict == "allowed":
                return "allow", str(rule or "allowed")
        verdict, reason = decide(tool, tool_input, d, repo, root)
        if verdict == "deny" and twin is not None and reason == NO_RULE_REASON:
            return decide(tool, {**tool_input, "command": twin}, d, repo, root)
        return verdict, reason
    if tool.startswith("mcp__"):
        verdict, _rule = permissions.mcp_verdict(tool, rules["allow"], rules["deny"])
        if verdict == "denied":
            return "deny", "Denied by dispatch MCP rule"
        if verdict == "allowed":
            return "allow", "Allowed by dispatch MCP rule"
        return "deny", "No dispatch MCP rule allows this tool"
    return "deny", "Unexpected tool in Codex dispatch policy hook"


def run_event(event: dict[str, Any]) -> dict[str, Any]:
    root = Path(os.environ["AGENT_ROOT"])
    dispatch_id = os.environ["AGENT_DISPATCH_ID"]
    verdict, reason = decision(root, dispatch_id, event)
    from ..hooks.decision_log import shown
    from .supervisor import record_permission_event

    d = state.load(root, dispatch_id)
    record_permission_event(
        root,
        d,
        {
            "ts": iso(now()),
            "tool": permissions.shown_tool(str(event.get("tool_name") or "")),
            "input": "[tool arguments omitted]",
            "input_omitted": True,
            "decision": verdict,
            "reason": shown(reason) if verdict == "deny" else "Codex policy hook allowed this call.",
            **_redacted_identities(event),
        },
    )
    if verdict == "allow":
        # Codex currently treats permissionDecision:allow without updatedInput as
        # invalid. A neutral response lets the normal sandbox policy proceed.
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": permissions.shown_text(reason),
        }
    }


#: Paths Codex's own sandbox protects inside a worktree, besides the owner's ask rules.
CODEX_PROTECTED = (".git", ".git/**", ".codex/**", ".agents/**")
_CLEANUP_OPTIONS = {"restore": {"--worktree", "-W"}, "checkout": set[str]()}
#: Characters a shell would expand or treat specially in a path: refused outright, so the path
#: checked is the path git receives (`src/${T:-../other/file}`).
_SHELL_SPECIAL = set("$`~{}*?[]\\!\n")


def cleanup_refusal(root: Path, d: dict[str, Any], parts: list[str]) -> str:
    """Explain why a worktree-only restore cannot be approved, or return empty text. Sandbox Git needs
    escalation to lock the index even when undoing a review probe.

    Restore literal in-worktree paths to HEAD only. Reject staged/source selection, pathspec files
    or magic, globbing, patch mode, branches and other options. Respect protected paths and a
    dispatch's declared scope. Read-only reviews and unknown scopes have no reserved paths but
    still obey protection.
    """
    from ..worktree_grants import protected_paths
    from .write_scope import covered

    verb, args = parts[1], parts[2:]
    if any(ch in arg for arg in args for ch in _SHELL_SPECIAL):
        return "a cleanup path may not use shell expansion, quoting tricks or patterns"
    if "--" not in args:
        return "a cleanup must separate its paths with `--`"
    split = args.index("--")
    options, paths = args[:split], args[split + 1 :]
    if set(options) - _CLEANUP_OPTIONS[verb] or not paths:
        return f"only `git {verb} {'[--worktree] ' if verb == 'restore' else ''}-- <paths>` is approved"
    worktree = Path(str(d["worktree"])).resolve()
    try:
        protected = [*CODEX_PROTECTED, *protected_paths(root)]
    except AgentError as exc:
        return f"cannot read the protected paths ({exc.message})"
    writes = d.get("writes")
    for raw in paths:
        if raw.startswith(":"):
            return f"`{raw}` is a pathspec; name each file"
        lexical = Path(os.path.normpath(worktree / raw))
        target = (worktree / raw).resolve()
        if target != lexical:
            return f"`{raw}` goes through a symlink or out of the worktree"
        if not target.is_relative_to(worktree) or target == worktree:
            return f"`{raw}` is not a file inside the worktree"
        rel = target.relative_to(worktree).as_posix()
        if _head_kind(worktree, rel) != "blob":
            # A directory restores everything below it, protected files included.
            return f"`{rel}` is not a file in HEAD; restore files one by one"
        if any(fnmatch.fnmatchcase(rel, pattern) or rel == pattern for pattern in protected):
            return f"`{rel}` is protected"
        if writes and not covered(rel, [str(w) for w in writes]):
            return f"`{rel}` is outside this dispatch's writes"
    return ""


def _head_kind(worktree: Path, rel: str) -> str:
    """`blob`, `tree` or "" for `rel` in the worktree's HEAD."""
    try:
        return subprocess.run(
            ["git", "-C", str(worktree), "cat-file", "-t", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return ""


_POSIX_WRAPPERS = (["/bin/zsh", "-lc"], ["/bin/bash", "-lc"])
_WINDOWS_SHELLS = ("powershell.exe", "pwsh.exe")
# Every character a PowerShell body may hold. The checks below parse the body with POSIX rules,
# and PowerShell reads many characters differently: smart quotes end strings, `\"` is no escape,
# `(...)` runs a subexpression, `@x` splats, and `|`, `>`, `;`, `&`, CR and U+2028 separate or
# redirect (review of 043-codex-powershell-commit). Only plain ASCII words, spaces and straight
# quotes are left, so both parsers split the body the same way.
_POWERSHELL_BODY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _./:=,+-!?'\"")


def _wrapped(argv: list[str]) -> bool:
    """Recognise Codex shell wrappers: POSIX shell -lc and Windows PowerShell -Command."""
    if len(argv) != 3:
        return False
    if argv[:2] in _POSIX_WRAPPERS:
        return True
    return (
        argv[1] == "-Command"
        and PureWindowsPath(argv[0]).name.lower() in _WINDOWS_SHELLS
        and set(argv[2]) <= _POWERSHELL_BODY
    )


def _same_dir(cwd: str, worktree: str) -> bool:
    """Whether Codex's `cwd` is the worktree on disk. The live decline that made exact string
    equality too strict was not captured; the inferred cause is an unnormalized `workdir` such as
    `<worktree>/`, `<worktree>/.` or a symlinked alias. Only the filesystem decides: a lexical
    `normpath` would read `<worktree>/link/..` as the worktree even when `link` leaves it."""
    if not os.path.isabs(cwd):
        return False
    return os.path.realpath(cwd) == os.path.realpath(worktree)


def git_metadata_refusal(root: Path, dispatch_id: str, params: dict[str, Any]) -> str:
    """Which check refuses a Codex escalation, or "" when the host approves it: only a worker's
    allowed git add/commit, or a worktree-only restore of declared paths (`cleanup_refusal`), in
    its own worktree."""
    d = state.load(root, dispatch_id)
    if params.get("kind") not in (None, "command"):
        return "kind"
    if params.get("networkApprovalContext") is not None:
        return "network"
    cwd, worktree = params.get("cwd"), d.get("worktree")
    if not (isinstance(cwd, str) and isinstance(worktree, str) and _same_dir(cwd, worktree)):
        return "cwd"
    raw = params.get("command")
    if not isinstance(raw, str):
        return "command-type"
    try:
        argv = shlex.split(raw)
        command = argv[2] if _wrapped(argv) else raw
        parts = shlex.split(command)
    except ValueError:
        return "command-parse"
    if len(parts) < 3 or parts[:2] not in (
        ["git", "add"],
        ["git", "commit"],
        ["git", "restore"],
        ["git", "checkout"],
    ):
        return "verb"
    if parts[1] in _CLEANUP_OPTIONS and cleanup_refusal(root, d, parts):
        return "cleanup"
    from .. import hooks

    if hooks.is_chained(command):
        return "chained"
    verdict, _reason = decision(root, dispatch_id, {"tool_name": "Bash", "tool_input": {"command": command}})
    return "" if verdict == "allow" else "policy"


def main() -> int:
    try:
        event = as_dict_or_none(json.load(sys.stdin))
        if event is None:
            raise ValueError("hook input is not an object")
        result = run_event(event)
    except Exception as exc:  # noqa: BLE001 - a broken policy must refuse the tool
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Codex dispatch policy failed: {type(exc).__name__}",
            }
        }
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
