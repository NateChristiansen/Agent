# ruff: noqa
from __future__ import annotations
import contextlib
import copy
import json
import time
from pathlib import Path
from typing import Any
from ..records.narrow import as_dict
from . import decision_log, groups, toggles
from .activity import activity, codex_activity
from .ask_guard import ask_guard
from .cli_route import cli_route
from .command_guards import pattern_kill_guard, sandbox_help_guard, subagent_revert_guard
from .commit_guard import commit_guard
from .guards import mcp_stale, no_chaining, verify_pipe
from .naming import session_name
from .notes_sync import notes_sync_hook
from .patch_lease import protect_with_patch_leases
from .paths import add_hook_note
from .peer_route import peer_route, plan_mode_allow
from .peer_stop import peer_stop
from .plan_gate import plan_gate
from .prompt import codex_prompt, manager_prompt
from .reap import session_reap
from .repo_guards import pull_warn, push_guard
from .session import capture_hook, dispatch_inbox, render_after_plan_edit, session_start
from .stop import journal_pending, lease_yield, note_hook_denial, thrash_guard
from .worker import precompact, worker_stop

NAMES = (
    "session-start",
    "capture",
    "protect-generated",
    "no-chaining",
    "verify-pipe",
    "journal-pending",
    "mcp-stale",
    "pull-warn",
    "push-guard",
    "commit-guard",
    "pattern-kill-guard",
    "sandbox-help-guard",
    "subagent-revert-guard",
    "render-after-plan-edit",
    "dispatch-inbox",
    "manager-prompt",
    "codex-prompt",
    "worker-stop",
    "precompact",
    "thrash-guard",
    "lease-yield",
    "session-name",
    "activity",
    "codex-activity",
    "session-reap",
    "plan-gate",
    "peer-stop",
    "peer-route",
    "cli-route",
    "plan-mode-allow",
    "notes-sync",
    "ask-guard",
)
ALIASES: dict[str, str] = {
    name: "removed 2026-10-12"
    for name in (
        "done-unticked",
        "instruction-name-guard",
        "lease-nudge",
        "plan-gate-stop",
        "probe-cwd-guard",
        "style-guard",
        "subagent-row-guard",
        "syntax-guard",
    )
}
BOM = chr(65279)


def run(root: Path, name: str, raw_stdin: str) -> tuple[str, int]:
    if name in ALIASES:
        return ("", 0)
    raw_stdin = raw_stdin.lstrip(BOM)
    try:
        payload = as_dict(json.loads(raw_stdin)) if raw_stdin.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    group = groups.GROUPS.get(name)
    if group is not None:
        try:
            return (run_group(root, group, payload), 0)
        except Exception:
            return ("", 0)
    return (run_one(root, name, payload), 0)


def run_one(root: Path, name: str, payload: dict[str, Any]) -> str:
    try:
        handler = HANDLERS[name]
    except KeyError:
        return ""
    if toggles.disabled(root, name):
        return ""
    started = time.monotonic()
    try:
        out = handler(root, payload)
    except Exception as exc:
        decision_log.error(root, name, payload, exc, time.monotonic() - started)
        return ""
    finally:
        with contextlib.suppress(Exception):
            from ..vault import live

            live.publish_pending(root)
    decision_log.denial(root, name, payload, out, time.monotonic() - started)
    if name not in decision_log.CONTINUATION_HOOKS:
        with contextlib.suppress(Exception):
            event = str(payload.get("hook_event_name") or "")
            out = add_hook_note(out, name, name in toggles.TOGGLEABLE, event)
    try:
        return note_hook_denial(root, payload, out)
    except Exception:
        return out


def run_group(root: Path, group: groups.Group, payload: dict[str, Any]) -> str:
    event = str(payload.get("hook_event_name") or group.event)
    tool = str(payload.get("tool_name") or "")
    outputs: list[str] = []
    for member in group.members:
        if member.tools and (not groups.matches(member.tools, tool)):
            continue
        try:
            out = run_one(root, member.name, copy.deepcopy(payload))
        except Exception:
            out = ""
        outputs.append(out)
        if groups.stops(event, out):
            break
        if groups.blocks_stop(event, out):
            payload = {**payload, "_stop_blocked": True}
    return groups.combine(event, outputs)


HANDLERS = {
    "session-start": session_start,
    "capture": capture_hook,
    "protect-generated": protect_with_patch_leases,
    "no-chaining": no_chaining,
    "verify-pipe": verify_pipe,
    "journal-pending": journal_pending,
    "mcp-stale": mcp_stale,
    "pull-warn": pull_warn,
    "push-guard": push_guard,
    "commit-guard": commit_guard,
    "pattern-kill-guard": pattern_kill_guard,
    "sandbox-help-guard": sandbox_help_guard,
    "subagent-revert-guard": subagent_revert_guard,
    "render-after-plan-edit": render_after_plan_edit,
    "dispatch-inbox": dispatch_inbox,
    "manager-prompt": manager_prompt,
    "codex-prompt": codex_prompt,
    "worker-stop": worker_stop,
    "precompact": precompact,
    "thrash-guard": thrash_guard,
    "lease-yield": lease_yield,
    "session-name": session_name,
    "activity": activity,
    "codex-activity": codex_activity,
    "session-reap": session_reap,
    "plan-gate": plan_gate,
    "peer-stop": peer_stop,
    "peer-route": peer_route,
    "cli-route": cli_route,
    "plan-mode-allow": plan_mode_allow,
    "notes-sync": notes_sync_hook,
    "ask-guard": ask_guard,
}
