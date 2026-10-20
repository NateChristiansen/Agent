"""PreToolUse Bash guards for destructive or contaminated command launches."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..records.narrow import dig
from .paths import deny, shell_tokens

PATTERN_KILL_REASON = (
    "pkill and killall can terminate another session's processes. "
    "Find the process owned by this session and use kill with its exact PID."
)


def pattern_kill_guard(root: Path, payload: dict[str, Any]) -> str:
    """Deny name-based process kills at the Bash tool boundary."""
    if str(payload.get("tool_name") or "") != "Bash":
        return ""
    command = str(dig(payload, "tool_input", "command") or "")
    if _is_pattern_kill(shell_tokens(command)):
        return deny(PATTERN_KILL_REASON)
    return ""


def _is_pattern_kill(tokens: list[str]) -> bool:
    """Unwrap common command launchers without mistaking `rg pkill` for a kill."""
    tokens = list(tokens)
    while tokens:
        name = Path(tokens[0]).name
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]) or name in {"env", "command", "nohup"}:
            tokens.pop(0)
            continue
        if name == "sudo":
            tokens.pop(0)
            while tokens and tokens[0] in {"-n", "-E", "--non-interactive", "--preserve-env"}:
                tokens.pop(0)
            continue
        if name in {"sh", "bash", "zsh"} and len(tokens) >= 3 and tokens[1] == "-c":
            return _is_pattern_kill(shell_tokens(tokens[2]))
        return name in {"pkill", "killall"}
    return False


_SANDBOX_OPTIONS_WITH_VALUE = {
    "-c",
    "--config",
    "--sandbox-state-json",
    "--sandbox-state-readable-root",
    "--enable",
    "--disable",
    "-P",
    "--permission-profile",
    "-p",
    "--profile",
    "-C",
    "--cd",
    "--allow-unix-socket",
}


def sandbox_help_guard(root: Path, payload: dict[str, Any]) -> str:
    """`codex sandbox` treats a word before --help as the command to execute."""
    if str(payload.get("tool_name") or "") != "Bash":
        return ""
    tokens = shell_tokens(str(dig(payload, "tool_input", "command") or ""))
    if len(tokens) < 3 or Path(tokens[0]).name != "codex" or tokens[1] != "sandbox":
        return ""
    i = 2
    while i < len(tokens):
        token = tokens[i]
        if token in ("-h", "--help"):
            return ""
        if token in _SANDBOX_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        if any(value in ("-h", "--help") for value in tokens[i + 1 :]):
            return deny(
                "codex sandbox received --help after its command payload; that help flag would be executed "
                "inside the sandbox. Run `codex sandbox --help` to inspect the parent grammar first."
            )
        return ""
    return ""


def subagent_revert_guard(root: Path, payload: dict[str, Any]) -> str:
    """A prose assignment gives a subagent no trusted revert scope."""
    if str(payload.get("tool_name") or "") != "Bash":
        return ""
    if not (os.environ.get("CLAUDE_AGENT_ID") or payload.get("agent_id")):
        return ""
    tokens = shell_tokens(str(dig(payload, "tool_input", "command") or ""))
    if not tokens or Path(tokens[0]).name != "git":
        return ""
    i = 1
    while i < len(tokens):
        if tokens[i] in {"-C", "-c", "--git-dir", "--work-tree"}:
            i += 2
        elif tokens[i].startswith("-"):
            i += 1
        else:
            break
    verb = tokens[i] if i < len(tokens) else ""
    if verb == "restore" or (verb == "checkout" and "--" in tokens[i + 1 :]):
        return deny(
            "a subagent cannot revert files because its assignment has no trusted path scope; "
            "ask the manager to review and perform the revert"
        )
    return ""
