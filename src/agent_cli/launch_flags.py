"""Validate the raw Claude argv escape hatch before SDK serialization.

Inventory: Claude Code 2.1.277 --help and claude-agent-sdk 0.2.152 subprocess_cli.py
_build_command. This rejects known conflicting controls, not every future native option or
abbreviation. Unknown well-formed long options keep their existing passthrough contract.
No native duplicate-option precedence or effective permission enforcement is inferred here.
"""

from __future__ import annotations

import re

from .records.narrow import as_list_or_none, str_list

# Canonical names are ours for comparison, not guesses about native case normalization.
# Only aliases actually shown by the pinned CLI are normalized.
ALIASES = {"allowedTools": "allowed-tools", "disallowedTools": "disallowed-tools", "bg": "background"}
SHORT_ALIASES = {
    "c": "continue",
    "d": "debug",
    "h": "help",
    "n": "name",
    "p": "print",
    "r": "resume",
    "v": "version",
    "w": "worktree",
}
MANAGED_GROUPS = {
    "permission and tool controls": frozenset(
        {
            "add-dir",
            "allowed-tools",
            "disallowed-tools",
            "tools",
            "permission-mode",
            "permission-prompts",
            "permission-prompt-tool",
            "dangerously-skip-permissions",
            "allow-dangerously-skip-permissions",
            "brief",
        }
    ),
    "settings, MCP and worker context": frozenset(
        {
            "settings",
            "setting-sources",
            "mcp-config",
            "strict-mcp-config",
            "agent",
            "agents",
            "plugin-dir",
            "plugin-url",
            "chrome",
            "ide",
            "bare",
            "restricted",
            "safe-mode",
            "disable-slash-commands",
            "system-prompt",
            "system-prompt-file",
            "append-system-prompt",
            "append-system-prompt-file",
            "system-prompt-snapshot",
            "exclude-dynamic-system-prompt-sections",
        }
    ),
    "session identity and execution location": frozenset(
        {
            "name",
            "session-id",
            "continue",
            "resume",
            "fork-session",
            "resume-session-at",
            "resume-drops-turn",
            "session-mirror",
            "no-session-persistence",
            "from-pr",
            "teleport",
            "worktree",
            "tmux",
            "background",
            "cloud",
            "environment",
            "remote-control",
            "remote-control-session-name-prefix",
        }
    ),
    "SDK transport and return format": frozenset(
        {
            "input-format",
            "output-format",
            "json-schema",
            "print",
            "replay-user-messages",
            "help",
            "version",
        }
    ),
    "model, effort and execution limits": frozenset(
        {
            "model",
            "fallback-model",
            "effort",
            "max-budget-usd",
            "max-turns",
            "task-budget",
            "thinking",
            "max-thinking-tokens",
            "thinking-display",
        }
    ),
}
MANAGED = {flag: group for group, flags in MANAGED_GROUPS.items() for flag in flags}

# Only known nonconflicting options get arity checks. A variadic native option can use
# one comma-separated value where supported (as the SDK does for betas); raw extra_args
# cannot faithfully represent multiple bare values or repeated options in its dict.
REQUIRED_VALUE = frozenset({"autocompact", "betas", "debug-file", "file"})
NO_VALUE = frozenset(
    {
        "ax-screen-reader",
        "forward-subagent-text",
        "include-hook-events",
        "include-partial-messages",
        "no-chrome",
        "verbose",
    }
)
OPTIONAL_VALUE = frozenset({"debug", "prompt-suggestions"})
KNOWN = frozenset(MANAGED) | REQUIRED_VALUE | NO_VALUE | OPTIONAL_VALUE
_LONG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]*\Z")


def _error(reason: str, flag: str | None = None) -> ValueError:
    """Only fixed known option names may appear; never reflect unknown input or values."""
    label = f" --{flag}" if flag in KNOWN else ""
    return ValueError(f"dispatch.extra_args{label}: {reason}")


def parse_extra_args(raw: object) -> dict[str, str | None]:
    """Parse exactly represented options, refusing conflicts and ambiguous argv shapes.

    Values beginning with '-' must use an explicit equals form. Literal values are never
    split as shell text; the pinned SDK also binds dash-leading values with '='. Short
    options are accepted only as documented standalone aliases, never bundled/attached.
    """
    if raw is None:
        return {}
    items = as_list_or_none(raw)
    if items is None or not all(
        isinstance(token, str) and token.strip() and "\0" not in token for token in items
    ):
        raise ValueError("dispatch.extra_args must be a list of non-empty raw Claude arguments without NUL")
    tokens = str_list(items)
    result: dict[str, str | None] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        spelling, separator, inline_value = token.partition("=")
        if spelling.startswith("--") and _LONG_NAME.fullmatch(spelling[2:]):
            flag = ALIASES.get(spelling[2:], spelling[2:])
        elif spelling.startswith("-") and spelling[1:] in SHORT_ALIASES:
            flag = SHORT_ALIASES[spelling[1:]]
        else:
            raise _error("expected a long option or a documented standalone short alias; raw text omitted")
        if flag in MANAGED:
            raise _error(f"conflicts with harness-managed {MANAGED[flag]}", flag)
        if flag in result:
            raise _error("duplicate option cannot be represented without losing argv intent", flag)
        value: str | None = None
        if separator:
            if flag in NO_VALUE:
                raise _error("does not accept a value", flag)
            if flag in REQUIRED_VALUE and not inline_value:
                raise _error("requires a non-empty value", flag)
            value = inline_value
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            if flag in NO_VALUE:
                raise _error("does not accept a value; positional arguments are not supported", flag)
            index += 1
            value = tokens[index]
        elif flag in REQUIRED_VALUE:
            raise _error("requires a value; bind dash-leading values with an explicit equals form", flag)
        result[flag] = value
        index += 1
    return result
