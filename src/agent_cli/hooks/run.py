"""The `bin/agent hook <name>` entry point: `NAMES` (the hook names), `run()` (stdin JSON in,
hook output out, never a non-zero exit), and `HANDLERS`, the name-to-function table every other
module here fills in. The client configs wire the groups in `groups.py`, which run these hooks
in-process (`run_group`); each name here stays a valid command on its own.
"""

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
from .record_guard import record_guard
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
    "journal-pending",  # UserPromptSubmit: milestones landed today with no journal note yet
    "mcp-stale",  # PreToolUse mcp__agent__*: the server predates the code it would run
    "pull-warn",  # PreToolUse Read|Grep|Glob: first read under a clone not pulled here
    "push-guard",  # PreToolUse Bash: `git push` of a branch with no review dispatch
    "commit-guard",  # PreToolUse Bash: shared append-only staged files require a file lease
    "pattern-kill-guard",  # PreToolUse Bash: deny machine-wide pkill/killall
    "sandbox-help-guard",  # PreToolUse Bash: reject a help flag inside codex sandbox's payload
    "subagent-revert-guard",  # PreToolUse Bash: no unscoped subagent file reverts
    "render-after-plan-edit",
    "dispatch-inbox",  # UserPromptSubmit: finished or blocked workers, only when something is pending
    "manager-prompt",  # Claude: inbox then activity, so blocked wakes cannot mark the tab working
    "codex-prompt",  # Codex: the same ordered pair, with Codex registration on allowed prompts
    "worker-stop",  # Stop/SessionEnd inside a dispatched worker (wired by generated settings)
    "precompact",  # PreCompact inside a dispatched worker: re-inject the nonce and return shape
    "thrash-guard",  # PermissionDenied/PostToolUseFailure/PostToolUse: repetition detector
    "lease-yield",  # Stop: stamp the turn end, so this session's leases read resting
    "session-name",  # UserPromptSubmit: title a mapped manager session mgr-NNN-slug
    "activity",  # prompt/stop/permission/question events: what the session is doing, on the Dashboard
    "codex-activity",  # same state machine, plus Codex tab registration and first claim
    "session-reap",  # SessionEnd: kill processes that escaped this session, by its env tag
    "plan-gate",  # PreToolUse ExitPlanMode: a plan reaches the owner after its fresh cross-client review
    "peer-stop",  # Claude Stop: hand over peer messages still unread at turn end
    "peer-route",  # Claude PreToolUse SendMessage: a manager is reached with `peer send`
    "cli-route",  # PreToolUse Bash: models never run bin/agent; name the MCP tool instead
    "plan-mode-allow",  # Claude PreToolUse: peer, mapping and plan-review dispatch allowed in plan mode
    "notes-sync",  # Stop: commit and publish the notes repo; UserPromptSubmit: pull and report (see ADR-0034)
    "ask-guard",  # Stop: a reply ending in an unregistered question to the owner is blocked once
    "record-guard",  # Stop: a manager owing reconciliation items is blocked once per turn and owed set
)

#: Keep removed hook names as no-op aliases for one release so cached client wiring stays usable.
#: Each value identifies the removal release; drop it in the next release. tests/hook_names.json
#: pins compatibility names. toggles.RETIRED separately preserves removed configuration switches
#: permanently.
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


BOM = chr(0xFEFF)  # spelled as a code point: the literal character is invisible in review


def run(root: Path, name: str, raw_stdin: str) -> tuple[str, int]:
    """Dispatch a hook or a hook group (`groups.GROUPS`). Returns (stdout text, exit code).
    Exit code is always 0. An `ALIASES` name does nothing."""
    if name in ALIASES:
        return "", 0
    # Windows PowerShell 5.1 prepends a byte-order mark when it pipes text to a native program.
    # json.loads refuses it, and the hook would then run on an empty payload and quietly allow
    # everything.
    raw_stdin = raw_stdin.lstrip(BOM)
    try:
        payload = as_dict(json.loads(raw_stdin)) if raw_stdin.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    group = groups.GROUPS.get(name)
    if group is not None:
        try:
            return run_group(root, group, payload), 0
        except Exception:  # noqa: BLE001 — the combiner never costs the session its turn
            return "", 0
    return run_one(root, name, payload), 0


def run_one(root: Path, name: str, payload: dict[str, Any]) -> str:
    """One hook, as `agent hook <name>` runs it: its switch, its failure log, the switch or
    protected note on a deny, and the denial count. Never raises."""
    try:
        handler = HANDLERS[name]
    except KeyError:
        return ""
    if toggles.disabled(root, name):
        return ""
    started = time.monotonic()
    try:
        out = handler(root, payload)
    except Exception as exc:  # noqa: BLE001 — hooks must fail open, but the failure is logged
        decision_log.error(root, name, payload, exc, time.monotonic() - started)
        return ""
    finally:
        # a fact this hook persisted under an intent is published before it returns
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
    except Exception:  # noqa: BLE001
        return out


def run_group(root: Path, group: groups.Group, payload: dict[str, Any]) -> str:
    """Run `group`'s members in order, in this thread, each on its own copy of the payload, and
    combine what they said (`groups.combine`). A member whose old matcher does not select this
    tool is skipped; a PreToolUse deny or a UserPromptSubmit block ends the run.

    No per-member thread budget: a timed-out thread kept running and mutating shared state (a
    drained inbox, a held session lock) after its answer was discarded, and logging its timeout
    could wait on the lock it held. Members run to completion one after
    another; a Claude group's wired timeout is the sum of their old ones plus startup, a Codex
    group's is Codex's 600 s default (`groups.Group.timeout`), and a member that raises fails
    open alone, as `run_one` always did. Slow members (notes-sync) are not in a group."""
    event = str(payload.get("hook_event_name") or group.event)
    tool = str(payload.get("tool_name") or "")
    outputs: list[str] = []
    for member in group.members:
        if member.tools and not groups.matches(member.tools, tool):
            continue
        try:
            out = run_one(root, member.name, copy.deepcopy(payload))
        except Exception:  # noqa: BLE001 — one member failing never costs the others
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
    "record-guard": record_guard,
}
