"""`cli-route`: models never run `bin/agent` in a shell.

An audit found managers on both clients running the CLI hundreds of times where a typed
MCP tool existed. Harness text told them to, MCP errors named CLI flags, and after an error they
fell back to the shell. The owner chose a full ban for models: every action a model runs has an
MCP tool (`mcp/coverage.py`), and this PreToolUse Bash hook refuses the rest, naming the tool.

It guards model tool calls only: Claude `Bash`, and each Codex `exec_command` the `Bash` matcher
sees. Client hooks, git hooks, the dispatch supervisor, and a person in a terminal still run the
CLI. It is a client hook guard, not a process boundary: a tool path a client does not route
through PreToolUse is not seen.

There is no CLI fallback when the MCP server is down. That fallback is how 38 of the audited
calls happened, and the CLI runs the same code, so it rarely helps. The refusal says to stop and
ask the owner to reconnect.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from ..mcp import coverage
from ..records.narrow import dig
from .paths import deny

OPERATORS = {"|", "||", "&&", ";", "&", "(", ")", ";;", "|&"}
GLOBAL_WITH_ARG = {"--root"}
WRAPPERS = {"env", "command", "nohup", "exec", "time"}
ENV_WITH_ARG = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-a"}
UV_RUN_WITH_ARG = {
    "--directory",
    "--project",
    "--package",
    "--with",
    "--with-requirements",
    "--python",
    "-p",
    "--env-file",
    "--extra",
    "--group",
    "--index",
    "--config-file",
}

DOWN_HINT = (
    "If the agent MCP server is unavailable, stop and ask the owner to reconnect it "
    "(`/mcp` in Claude Code, a restart in Codex). Do not fall back to the CLI."
)


def segments(command: str) -> list[list[str]]:
    """Simple commands in one shell command line, split on operators outside quotes, with
    redirections dropped. A line the lexer cannot parse comes back as whitespace words."""
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return [command.split()]
    out: list[list[str]] = [[]]
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in OPERATORS:
            out.append([])
        elif set(tok) <= set("<>&|") and ("<" in tok or ">" in tok):
            if out[-1] and out[-1][-1].isdigit():
                out[-1].pop()
            skip_next = True
        else:
            out[-1].append(tok)
    return [s for s in out if s]


def _is_agent_binary(word: str) -> bool:
    """`bin/agent`, `./bin/agent`, an absolute path to it, `.venv/bin/agent`, `bin/agent.cmd`."""
    name = Path(word.replace("\\", "/")).name
    return name in {"agent", "agent.cmd", "agent.ps1"} and "bin" in Path(word.replace("\\", "/")).parts


def skip_options(words: list[str], with_arg: set[str]) -> list[str]:
    """Drop leading options (and the value of each one in `with_arg`) up to the first operand."""
    while words and words[0].startswith("-") and words[0] != "-":
        flag = words.pop(0)
        if flag in with_arg and "=" not in flag and words:
            words.pop(0)
    return words


def _agent_args(words: list[str]) -> list[str] | None:
    """The CLI arguments when this simple command runs the agent CLI, else None."""
    words = list(words)
    while words:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words.pop(0)
        elif Path(words[0]).name in WRAPPERS:
            words = skip_options(words[1:], ENV_WITH_ARG)
        else:
            break
    if not words:
        return None
    head = Path(words[0]).name
    if words[:2] == ["uv", "run"]:
        rest = skip_options(words[2:], UV_RUN_WITH_ARG)
        return rest[1:] if rest and Path(rest[0]).name == "agent" else None
    if head in {"sh", "bash", "zsh", "dash", "ksh"}:
        # `-c`, or a flag group holding c (`-lc`, `-ic`, `-xc`): the next word is the script.
        i = next(
            (k for k, w in enumerate(words[1:], 1) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", w)),
            None,
        )
        if i is None or i + 1 >= len(words):
            return None
        for inner in segments(words[i + 1]):
            found = _agent_args(inner)
            if found is not None:
                return found
        return None
    if _is_agent_binary(words[0]):
        return words[1:]
    if head.startswith("python") and words[1:3] == ["-m", "agent_cli"]:
        return words[3:]
    return None


def _action(args: list[str]) -> tuple[str | None, str | None]:
    rest = list(args)
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in GLOBAL_WITH_ARG and rest:
            rest.pop(0)
    if not rest:
        return None, None
    return rest[0], next((w for w in rest[1:] if not w.startswith("-")), None)


def reason_for(cmd: str | None, sub: str | None) -> str:
    """The refusal for `bin/agent cmd sub`: the MCP tool to use, or who runs it instead."""
    shown = " ".join(p for p in ("bin/agent", cmd, sub) if p)
    lead = f"Models never run `bin/agent` in a shell; `{shown}` was refused."
    found = coverage.tool_for(cmd, sub)
    if found is not None:
        tool, action = found
        call = f"the agent MCP tool `{tool}`" + (f" with action `{action}`" if action else "")
        return f"{lead} Use {call}; its description lists the parameters. {DOWN_HINT}"
    if coverage.person_only(cmd, sub):
        return (
            f"{lead} `{shown}` is person-only. Give the owner the exact command to run in a "
            f"terminal (or with `!` in Claude Code), and wait for the result."
        )
    return (
        f"{lead} Use the agent MCP tools; each tool's description documents its parameters, so "
        f"`--help` is not needed. If no tool fits, ask the owner. {DOWN_HINT}"
    )


def cli_route(root: Path, payload: dict[str, Any]) -> str:
    if str(payload.get("tool_name") or "") != "Bash":
        return ""
    command = str(dig(payload, "tool_input", "command") or "")
    if "agent" not in command:
        return ""
    for words in segments(command):
        args = _agent_args(words)
        if args is not None:
            return deny(reason_for(*_action(args)))
    return ""
