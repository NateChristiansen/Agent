"""Run each client event and matcher in one hook process to avoid repeated loader and interpreter
startup. Members execute sequentially with run.run_one semantics: switches, failure logging, notes
and denial counts. Standalone names stay valid for cached wiring. Changed Codex wiring requires
re-trust in /hooks.

Preserve each member's matcher. Claude group timeouts sum member budgets plus STARTUP_S; Codex
retains its 600 s default. Do not abandon timed-out member threads: they could keep mutating shared
state after their answer is discarded.

Combine outputs in member order. PreToolUse stops on the first deny, otherwise returns the first
ask. UserPromptSubmit stops on the first block. Stop runs all members and joins block reasons,
passing _stop_blocked onward so activity stays working and leases stay held; ask-guard runs first,
then record-guard.
Join additionalContext in order and keep the first value of other fields. Return lone outputs
unchanged.

Keep notes-sync separate because its network work would hold up all members. Keep SessionEnd hooks
separate because Codex gives each only 1 s. Import no handlers here so group metadata stays cheap
to read.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from ..records.narrow import as_dict, as_dict_or_none, as_str, dict_list

#: Interpreter start and output, on top of the member budgets, in a Claude group's wired timeout.
STARTUP_S = 5
#: Codex's timeout for a hook command that sets none: what each Codex member had before grouping.
CODEX_DEFAULT_S = 600


@dataclass(frozen=True)
class Member:
    name: str
    budget: float
    tools: str = ""  # the member's own old matcher, when narrower than the group's


#: ask-guard reads the reply and updates one record field within STARTUP_S. Its zero budget leaves
#: wired Claude timeouts unchanged. Both Stop groups share its capped announcement and
#: missing-command checks.
ASK_GUARD = Member("ask-guard", 0)
#: record-guard folds the feature's ledger, which heals pending intents first; it follows ask-guard
#: so an ask block spends nothing of it.
RECORD_GUARD = Member("record-guard", 5)


@dataclass(frozen=True)
class Group:
    name: str
    client: str  # "claude" or "codex"
    event: str
    matcher: str | None
    members: tuple[Member, ...]
    status: str = ""  # Claude's statusMessage

    @property
    def timeout(self) -> int:
        if self.client == "codex":
            return CODEX_DEFAULT_S
        return math.ceil(sum(m.budget for m in self.members)) + STARTUP_S


def _bash(protect_first: bool) -> tuple[Member, ...]:
    """The shell guards in their old wiring order. Claude listed `protect-generated` first (its
    own `Edit|Write|NotebookEdit|Bash` entry came before the Bash block); Codex listed it last."""
    guards = tuple(
        Member(name, 5)
        for name in (
            "no-chaining",
            "verify-pipe",
            "push-guard",
            "commit-guard",
            "pattern-kill-guard",
            "sandbox-help-guard",
            "subagent-revert-guard",
            "cli-route",
        )
    )
    protect = (Member("protect-generated", 5),)
    return protect + guards if protect_first else guards + protect


_GROUPS = (
    # ---- Claude (.claude/settings.json)
    Group(
        "start",
        "claude",
        "SessionStart",
        "startup|resume|clear|compact|fork",
        (Member("session-start", 60), Member("capture", 5), Member("activity", 5)),
        "Loading session feature...",
    ),
    Group(
        "prompt-submit",
        "claude",
        "UserPromptSubmit",
        None,
        (
            Member("capture", 5),
            ASK_GUARD,
            Member("manager-prompt", 10),
            Member("journal-pending", 5),
            Member("session-name", 5),
        ),
    ),
    Group(
        "stop",
        "claude",
        "Stop",
        None,
        (
            Member("capture", 10),
            Member("peer-stop", 5),
            ASK_GUARD,
            RECORD_GUARD,
            Member("lease-yield", 5),
            Member("activity", 5),
        ),
    ),
    Group(
        "pre-mcp",
        "claude",
        "PreToolUse",
        "mcp__agent__.*",
        (
            Member("mcp-stale", 5),
            Member("plan-mode-allow", 5, "mcp__agent__(peer|feature_use|feature_show|session_get|dispatch)"),
        ),
    ),
    Group("pre-bash", "claude", "PreToolUse", "Bash", _bash(True), "Checking the shell command..."),
    Group(
        "pre-ask",
        "claude",
        "PreToolUse",
        "AskUserQuestion|ExitPlanMode",
        (
            Member("plan-gate", 5, "ExitPlanMode"),
            Member("activity", 5),
            Member("ask-guard", 0, "AskUserQuestion"),  # notes the question's text
        ),
    ),
    Group(
        "post-tool",
        "claude",
        "PostToolUse",
        ".*",
        (
            Member("render-after-plan-edit", 10, "Edit|Write"),
            Member("thrash-guard", 5, "Bash|Edit|Write"),
            Member("activity", 5),
        ),
    ),
    # ---- Codex (.codex/hooks.json)
    Group(
        "codex-start",
        "codex",
        "SessionStart",
        None,
        (Member("session-start", 60), Member("capture", 5), Member("codex-activity", 5)),
    ),
    Group(
        "codex-prompt-submit",
        "codex",
        "UserPromptSubmit",
        None,
        (Member("capture", 5), ASK_GUARD, Member("codex-prompt", 60), Member("journal-pending", 5)),
    ),
    Group("codex-pre-bash", "codex", "PreToolUse", "Bash", _bash(False)),
    Group(
        "codex-stop",
        "codex",
        "Stop",
        None,
        (
            Member("capture", 10),
            ASK_GUARD,
            RECORD_GUARD,
            Member("lease-yield", 5),
            Member("codex-activity", 5),
        ),
    ),
    # Codex plan patches re-render just as Claude Edit/Write events do in post-tool.
    Group(
        "codex-post-tool",
        "codex",
        "PostToolUse",
        None,
        (Member("codex-activity", 5), Member("render-after-plan-edit", 10, "apply_patch")),
    ),
)

GROUPS: dict[str, Group] = {g.name: g for g in _GROUPS}
NAMES = tuple(GROUPS)

_SIMPLE = re.compile(r"[A-Za-z0-9_|]+")


def matches(matcher: str | None, tool: str) -> bool:
    """Whether a client matcher selects `tool`, the way Claude Code reads one: empty or `*`
    selects every tool, a plain `A|B` list matches names exactly, anything else is a regular
    expression searched in the name."""
    if not matcher or matcher == "*":
        return True
    if _SIMPLE.fullmatch(matcher):
        return tool in matcher.split("|")
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def _parse(text: str) -> dict[str, object] | None:
    try:
        return as_dict_or_none(json.loads(text))
    except ValueError:
        return None


def _spec(data: dict[str, object] | None) -> dict[str, object]:
    return as_dict((data or {}).get("hookSpecificOutput"))


def stops(event: str, out: str) -> bool:
    """Whether `out` ends the group: a PreToolUse deny, or a UserPromptSubmit block."""
    if not out:
        return False
    data = _parse(out)
    if event == "PreToolUse":
        return _spec(data).get("permissionDecision") == "deny"
    if event == "UserPromptSubmit":
        return (data or {}).get("decision") == "block"
    return False


def blocks_stop(event: str, out: str) -> bool:
    """Whether `out` blocks a Stop, which continues the turn instead of ending it."""
    return event == "Stop" and bool(out) and (_parse(out) or {}).get("decision") == "block"


def combine(event: str, outputs: list[str]) -> str:
    """One hook output from the members' outputs, in member order (see the module docstring)."""
    said = [o for o in outputs if o]
    if len(said) <= 1:
        return said[0] if said else ""
    parsed = [_parse(o) for o in said]
    for wanted in ("deny", "ask"):
        for text, data in zip(said, parsed, strict=True):
            if _spec(data).get("permissionDecision") == wanted:
                return text
    if event == "UserPromptSubmit":
        for text, data in zip(said, parsed, strict=True):
            if (data or {}).get("decision") == "block":
                return text
    merged: dict[str, Any] = {}
    spec: dict[str, Any] = {}
    contexts: list[str] = []
    reasons: list[str] = []
    for text, data in zip(said, parsed, strict=True):
        if data is None:  # plain stdout is context to both clients
            contexts.append(text.strip())
            continue
        blocking = data.get("decision") == "block"
        for key, value in data.items():
            if key == "hookSpecificOutput":
                for k, v in _spec(data).items():
                    if k == "additionalContext":
                        if v:
                            contexts.append(str(v))
                    else:
                        spec.setdefault(k, v)
            elif key == "reason" and blocking:
                if value:
                    reasons.append(str(value))
            else:
                merged.setdefault(key, value)
    if reasons:
        merged["decision"] = "block"
        merged["reason"] = "\n\n".join(reasons)
    if contexts:
        spec["additionalContext"] = "\n\n".join(contexts)
    if spec:
        merged["hookSpecificOutput"] = {"hookEventName": spec.pop("hookEventName", event), **spec}
    return json.dumps(merged) if merged else ""


def command_name(command: str) -> str:
    """The hook name a wired command runs: the word after `agent hook`."""
    _, _, tail = command.partition(" agent hook ")
    return tail.split()[0] if tail.split() else ""


def wired(config: dict[str, Any], event: str, tool: str | None = None) -> list[str]:
    """The member hooks a client config runs for `event` (and `tool`), groups expanded in order.
    `tool=None` lists every member regardless of matchers."""
    names: list[str] = []
    for entry in dict_list(as_dict(config.get("hooks")).get(event)):
        if tool is not None and not matches(as_str(entry.get("matcher")), tool):
            continue
        for hook in dict_list(entry.get("hooks")):
            name = command_name(as_str(hook.get("command")))
            group = GROUPS.get(name)
            if group is None:
                names.append(name)
                continue
            names += [m.name for m in group.members if tool is None or matches(m.tools, tool)]
    return names
