# ruff: noqa
from __future__ import annotations
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from ..records.narrow import as_dict, as_dict_or_none, as_str, dict_list

STARTUP_S = 5
CODEX_DEFAULT_S = 600


@dataclass(frozen=True)
class Member:
    name: str
    budget: float
    tools: str = ""


ASK_GUARD = Member("ask-guard", 0)


@dataclass(frozen=True)
class Group:
    name: str
    client: str
    event: str
    matcher: str | None
    members: tuple[Member, ...]
    status: str = ""

    @property
    def timeout(self) -> int:
        if self.client == "codex":
            return CODEX_DEFAULT_S
        return math.ceil(sum((m.budget for m in self.members))) + STARTUP_S


def _bash(protect_first: bool) -> tuple[Member, ...]:
    guards = tuple(
        (
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
    )
    protect = (Member("protect-generated", 5),)
    return protect + guards if protect_first else guards + protect


_GROUPS = (
    Group(
        "start",
        "claude",
        "SessionStart",
        "startup|resume|clear|compact|fork",
        (Member("session-start", 60), Member("capture", 5), Member("activity", 5)),
        "Loading STATUS.md and session feature...",
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
            Member(
                "plan-mode-allow", 5, "mcp__agent__(peer|feature_use|feature_current|session_get|dispatch)"
            ),
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
            Member("ask-guard", 0, "AskUserQuestion"),
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
        (Member("capture", 10), ASK_GUARD, Member("lease-yield", 5), Member("codex-activity", 5)),
    ),
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
_SIMPLE = re.compile("[A-Za-z0-9_|]+")


def matches(matcher: str | None, tool: str) -> bool:
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
    if not out:
        return False
    data = _parse(out)
    if event == "PreToolUse":
        return _spec(data).get("permissionDecision") == "deny"
    if event == "UserPromptSubmit":
        return (data or {}).get("decision") == "block"
    return False


def blocks_stop(event: str, out: str) -> bool:
    return event == "Stop" and bool(out) and ((_parse(out) or {}).get("decision") == "block")


def combine(event: str, outputs: list[str]) -> str:
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
        if data is None:
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
    _, _, tail = command.partition(" agent hook ")
    return tail.split()[0] if tail.split() else ""


def wired(config: dict[str, Any], event: str, tool: str | None = None) -> list[str]:
    names: list[str] = []
    for entry in dict_list(as_dict(config.get("hooks")).get(event)):
        if tool is not None and (not matches(as_str(entry.get("matcher")), tool)):
            continue
        for hook in dict_list(entry.get("hooks")):
            name = command_name(as_str(hook.get("command")))
            group = GROUPS.get(name)
            if group is None:
                names.append(name)
                continue
            names += [m.name for m in group.members if tool is None or matches(m.tools, tool)]
    return names
