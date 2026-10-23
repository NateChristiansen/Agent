"""`protect-generated` and `no-chaining`: the two PreToolUse hooks that deny an edit or a Bash
call outright. `protect-generated` covers Edit/Write/MultiEdit/NotebookEdit (via `paths.py`'s
`check_path`/`check_command`) plus the per-session lease guard on a leaseable area
(`vault:src`, `vault:harness`). `no-chaining` is the one-command-per-Bash-call rule
(AGENTS.md standing rules), including the shell-wrapper and command-substitution cases.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .. import sessions
from ..quote_mask import mask_quotes
from ..records.narrow import as_dict
from .notes_paths import OWNER_FILES, generated_denial, notes_logical
from .paths import (
    PRODUCT_CODE_REASON,
    TMP_PREFIXES,
    ask,
    check_command,
    check_path,
    deny,
    drop_heredoc_bodies,
    relative,
    session_feature,
    shell_tokens,
    under_registered_repo,
)
from .readonly_chain import read_only_chain
from .worktree_guard import alias_denial, manager_worktree, worktree_denial

SHELL_WRAPPER_HEADS = {"bash", "sh", "zsh", "dash", "ksh", "eval", "source", "."}

# Four working sessions hit this denial 10, 8, 10 and 2 times. Each hit cost a retry;
# naming the first command to run in `respelling_hint` avoids another guessing turn.
NO_CHAIN_REASON = (
    "chained commands are not allowed (AGENTS.md standing rules: one command per shell call; "
    "no &&, ||, ;, &, newline-separated commands, command substitution, or inline shell "
    "scripts). Split into separate shell calls, in parallel when they are independent. Pipes "
    "are fine, and so is a chain of read-only ls, cat, head, tail, wc, echo, grep or jq joined "
    "by &&, ; or | with no $, redirection or substitution; git, rg, sed and find always run "
    "alone. To change a "
    "file, use the Edit tool (Codex: apply_patch), and redo a refused step rather than skipping it."
)

# ------------------------------------------------------------------ protect-generated


def _denies(result: str) -> bool:
    """A hook result that is a deny."""
    if not result:
        return False
    try:
        return json.loads(result)["hookSpecificOutput"]["permissionDecision"] == "deny"
    except KeyError, TypeError, ValueError:
        return False


def protect_generated(root: Path, payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool_name") or "")
    tool_input = as_dict(payload.get("tool_input"))
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        raw = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if not raw:
            return ""
        outside = under_registered_repo(root, raw)
        if outside:
            return deny(PRODUCT_CODE_REASON)
        # A manager worktree: judged by its own relative path, no lease.
        # A symlink out of it falls through and is judged by where it lands.
        written = manager_worktree(root, raw)
        if written is not None:
            denial = worktree_denial(raw, *written)
            if denial or written[1] is not None:
                return denial
        rel = relative(root, raw)
        if os.path.isabs(rel):
            landing = manager_worktree(root, rel)
            if landing is not None:
                return worktree_denial(raw, *landing)
            # A linked notes entry is the vault's own, however it is spelled: judge it by its
            # vault path, with the generated-file list the native rules apply.
            mapped = notes_logical(root, rel)
            if mapped is not None:
                if denial := generated_denial(mapped):
                    return denial
                leased = lease_guard(root, payload, mapped)
                if mapped.lower() in OWNER_FILES and not _denies(leased):
                    return ask(f"{raw} is the owner's file ({mapped}). Writing it needs the owner's ok.")
                return leased
            if rel.startswith(TMP_PREFIXES):
                return ""
            return ask(f"{raw} is outside the harness root. Writing there needs a human ok.")
        denial = alias_denial(root, raw, rel) or check_path(rel)
        if denial:
            return denial
        return lease_guard(root, payload, rel)
    if tool == "Bash":
        return check_command(root, str(tool_input.get("command") or ""))
    return ""


def lease_guard(root: Path, payload: dict[str, Any], rel: str) -> str:
    """Item 4: the first edit under a leaseable area (`vault:src`,
    `vault:harness`) registers this session's hold. A later edit just touches it.

    A second live session editing the same area is denied with the exact wait line
    `leases.take` would have raised (`leases.refusal`). The model gets one line instead of a
    traceback from a `take` call it never gets to make. Fails open: any error here (leases.py
    absent in an old worktree, a read racing a concurrent write) must never block an edit the
    rest of this hook already allowed. The whole point is to be a courtesy on top of that
    decision, not a new way to wedge it."""
    try:
        from .. import leases as leases_mod

        area = leases_mod.area_for_path(rel)
        if area is None:
            return ""
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return ""
        # A file lease this session already took stands in for the directory area, so a
        # manager that leased `hooks/commit_guard.py` is not blocked by a peer's lease on
        # `hooks/activity.py`.
        own = leases_mod.get(root, f"vault:{rel}")
        if own is not None and own.get("session_id") == session_id:
            area = f"vault:{rel}"
        pid = sessions.resolve_pid()  # None when unknown: a lease reads that, 0 would be malformed
        # Not this area's own file: an area is a path prefix, so what blocks the edit may be a
        # lease on a parent directory or on a narrower path inside it.
        existing = leases_mod.blocker(root, area, session_id)
        if existing is not None:
            held = str(existing.get("area") or area)
            # Record the denied session's need on the lease so its holder can yield at turn end
            # without owner mediation.
            leases_mod.request(root, held, session_id=session_id, pid=pid)
            return deny(leases_mod.refusal(root, held, existing))
        result = leases_mod.take(
            root, area, session_id=session_id, pid=pid, feature=session_feature(root, session_id)
        )
        if result.get("took_over_from"):
            line = (
                f"took {area} over from {leases_mod.holder_label(root, str(result['took_over_from']))}, "
                "which was resting, idle or gone"
            )
            return json.dumps(
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": line}}
            )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"note: lease check failed ({exc}); allowing the edit\n")
    return ""


# ------------------------------------------------------------------ no-chaining


def no_chaining(root: Path, payload: dict[str, Any]) -> str:
    tool_input = as_dict(payload.get("tool_input"))
    cmd = str(tool_input.get("command") or "")
    if not cmd.strip():
        return ""
    if is_chained(cmd) and not read_only_chain(cmd):
        reason = NO_CHAIN_REASON + respelling_hint(cmd)
        if sessions.resolve_provider() == "codex":
            # Codex appends ". Command: ..." to a hook denial. Keep the Claude wording
            # stable while giving Codex one period and its own shell terminology.
            reason = reason.replace("Bash call", "shell call").removesuffix(".")
        return deny(reason)
    return ""


# Commands whose *exit code* is the answer, not their output. A pipeline exits with its last
# segment's status, so `pytest | tail` reports tail's success and a failing suite reads as a
# passing one.
VERIFY_HEADS = ("pytest", "ruff", "pyright", "mypy")
VERIFY_SUBCOMMANDS = (("agent", "eval"),)
# Only tools whose answer IS the exit code. `doctor` is deliberately absent:
# its answer is a list of rows, filtering them is the normal way to read them, and grepping
# `doctor` for FAIL is the correct read rather than a mistaken one. A guard that fires on the
# legitimate case gets switched off, and then it is not protecting the case it was built for.
PIPED_VERIFY_REASON = (
    "a pipeline exits with its LAST command's status, so this reports the exit code of the "
    "command after the pipe, not of the check itself. A failing suite reads as a passing one "
    "(three times in one session on 2026-09-23). Run it unpiped — long output is "
    "persisted to a file for you — or, if you only want the output and not the verdict, say so "
    "by sending the result somewhere explicit."
)


def verify_pipe(root: Path, payload: dict[str, Any]) -> str:
    """Deny piping a verification command into anything.

    The mistake this prevents is not misreading output, it is reading the wrong number: the
    harness reports a backgrounded command's exit code, and for `uv run pytest -q | tail -6`
    that code belongs to `tail`. It went wrong three times in one session, twice after being
    noticed and resolved against, which is the definition of something that needs a mechanism
    rather than a lesson.
    """
    tool_input = as_dict(payload.get("tool_input"))
    cmd = str(tool_input.get("command") or "")
    if "|" not in cmd or not cmd.strip():
        return ""
    text = drop_heredoc_bodies(cmd).replace("\\\n", " ")
    tokens = shell_tokens(text)
    if not tokens:
        return ""
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "|":
            segments.append([])
        else:
            segments[-1].append(tok)
    # only a segment with something after it: the last one owns the exit code and is fine
    for seg in segments[:-1]:
        if _is_verify(seg):
            return deny(PIPED_VERIFY_REASON)
    return ""


# Listing flags such as pytest --help, --version and --collect-only produce readable text; piping
# those does not hide a verification exit code.
OUTPUT_FLAGS = ("--help", "-h", "--version", "--collect-only", "--co", "--fixtures", "--markers")


def _is_verify(segment: list[str]) -> bool:
    """Whether this pipeline segment runs a check whose exit code is its answer. Runner
    prefixes (`uv run`, `uvx`, `poetry run`, `npx`) are skipped so the real head is found."""
    if any(t in OUTPUT_FLAGS for t in segment):
        return False
    words = [os.path.basename(t) for t in segment if not t.startswith("-")]
    while words and words[0] in ("uv", "uvx", "poetry", "npx", "pnpm", "npm", "run", "exec"):
        words.pop(0)
    if not words:
        return False
    if words[0] in VERIFY_HEADS:
        return True
    return any(words[0] == head and sub in words[1:] for head, sub in VERIFY_SUBCOMMANDS)


_SPLIT_OPERATOR_RE = re.compile(r"&&|\|\||\||;|&|\n")


def respelling_hint(cmd: str) -> str:
    """Offer the first unquoted command segment after a chaining denial. Return empty guidance for
    substitutions, shell wrappers or text with no safe split. Quoted operators are text and cannot
    supply a command boundary. Best effort only.
    """
    text = drop_heredoc_bodies(cmd).replace("\\\n", " ")
    m = _SPLIT_OPERATOR_RE.search(mask_quotes(text))
    if not m:
        return ""
    first = text[: m.start()].strip()
    if not first:
        return ""
    return f" Run `{first}` first, then the next command in a new Bash call."


def is_chained(cmd: str) -> bool:
    """True if the Bash command is more than one command.

    Checked, in order: heredoc bodies are dropped (the terminator and anything after it are
    still scanned). Backslash-newline continuations are joined. Command substitution
    (`$(...)`, backticks, `<(`, `>(`) outside single quotes counts. A shell wrapper with an
    inline script (`bash -c`, `sh -c`, `eval`, `source`) counts. Then the command is tokenized
    with shlex, and any `&&`, `||`, `;`, `&`, `|&` token or a newline between tokens counts.
    Quoted text is never unwrapped, so an operator inside quotes is an argument, not a chain,
    except inside a shell wrapper, which is refused outright.
    """
    text = drop_heredoc_bodies(cmd).replace("\\\n", " ")
    if not text.strip():
        return False
    if _unquoted_operator(text):
        return True
    tokens = shell_tokens(text)
    if not tokens:
        return False
    # every pipeline segment has a head; an inline script after a pipe is still an inline script
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "|":
            segments.append([])
        else:
            segments[-1].append(tok)
    for seg in segments:
        if not seg:
            continue
        head = os.path.basename(seg[0])
        if head == "env":
            at = next((i for i, t in enumerate(seg) if os.path.basename(t) in SHELL_WRAPPER_HEADS), None)
            if at is not None and _inline_shell(seg[at:]):
                return True
        elif head in SHELL_WRAPPER_HEADS and _inline_shell(seg):
            return True
        if head in _RUNNER_HEADS and _runner_shell(seg):
            return True
    return False


_RUNNER_HEADS = ("uv", "uvx", "npx", "pnpm", "npm", "python", "python3")
#: A python one-liner that starts a shell is a chain in disguise (`os.system('a && b')`).
_PY_SHELL_RE = re.compile(r"\b(?:os\.system|os\.popen|os\.exec\w*|subprocess|pty\.spawn)\b")


def _runner_shell(seg: list[str]) -> bool:
    """Recognise runners hiding inline shell scripts and Python code that shells out. Plain Python -c
    and another program's -c configuration option are ordinary commands.
    """
    window = seg[1:5]
    if any(os.path.basename(w) in _SCRIPT_SHELLS and "-c" in window[i + 1 :] for i, w in enumerate(window)):
        return True
    for i, word in enumerate(seg[:5]):
        if os.path.basename(word) in ("python", "python3") and seg[i + 1 : i + 2] == ["-c"]:
            return bool(_PY_SHELL_RE.search(" ".join(seg[i + 2 : i + 3])))
    return False


_SCRIPT_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}


def _inline_shell(seg: list[str]) -> bool:
    """Recognise inline shell scripts through -c, clustered variants, -s or stdin. eval, source and
    dot always count. Script-file and --version invocations remain ordinary commands.
    """
    head = os.path.basename(seg[0])
    if head not in _SCRIPT_SHELLS:
        return True
    # A redirect (`<<EOF`, `< file`, `> out`) and its target are not arguments: without a
    # script file the shell reads its commands from them.
    args: list[str] = []
    skip = False
    for a in seg[1:]:
        if skip:
            skip = False
        elif a in ("<", "<<", "<<<", ">", ">>"):
            skip = True
        elif not a.startswith(("<", ">")) and "<<" not in a:
            args.append(a)
    if args in (["--version"], ["--help"]):
        return False
    # Options in order, as the shell reads them, up to the first operand, which is the script
    # file. -c or -s anywhere before it makes the call an inline script, whatever follows
    # (`bash -c "a; b" --version`). -o/-O and --rcfile/--init-file take an
    # operand of their own, which is not a script (`bash -o pipefail` still reads stdin).
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return i + 1 >= len(args)
        if a.startswith("--"):
            i += 2 if a in ("--rcfile", "--init-file") else 1
            continue
        if a[:1] in ("-", "+") and len(a) > 1:
            if "c" in a[1:] or "s" in a[1:]:
                return True
            i += 2 if ("o" in a[1:] or "O" in a[1:]) else 1
            continue
        return False
    return True


def _unquoted_operator(text: str) -> bool:
    """Scan with real quote tracking.

    True on an unquoted `&&`, `||`, `;`, `&` (background), `|&`, a newline between two
    commands, or command substitution anywhere it would run (`$(`, backticks, `<(`, `>(`
    outside single quotes). Unbalanced quotes count as chained: refuse rather than guess."""
    i, n = 0, len(text)
    in_single = in_double = False
    seen_token = False  # a command word has started on the current line
    pending_newline = False  # a newline was seen after a command word
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_double = False
            elif c == "`" or (c == "$" and nxt == "("):
                return True
            i += 1
            continue
        if c == "\\":
            seen_token = True
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            seen_token = True
            continue
        if c == '"':
            in_double = True
            i += 1
            seen_token = True
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\n"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "\n":
            if seen_token:
                pending_newline = True
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if c == "`" or (c == "$" and nxt == "(") or (c in "<>" and nxt == "("):
            return True
        if c == ";" or (c == "&" and nxt == "&") or (c == "|" and nxt == "|") or (c == "|" and nxt == "&"):
            return True
        if c == "&":
            prev = text[i - 1] if i > 0 else ""
            if prev in "<>" or nxt == ">":
                i += 1  # part of a redirect: 2>&1, &>, >&2
                continue
            return True
        if pending_newline:
            return True  # a second command on a new line
        seen_token = True
        i += 1
    return in_single or in_double


# ------------------------------------------------------------------ mcp-stale


def mcp_stale(root: Path, payload: dict[str, Any]) -> str:
    """Warn when a connected MCP server caches a contract that current source no longer supplies. Use
    additionalContext so the model sees restart guidance at the affected call. Do not deny
    unchanged operations or require reconnect for ordinary source edits.
    """
    tool = str(payload.get("tool_name") or "")
    if not tool.startswith("mcp__agent__"):
        return ""
    try:
        from ..mcp.staleness import stale_line, warned_before

        line = stale_line(root)
    except Exception:  # noqa: BLE001 — a hook never raises; failing open is the contract
        return ""
    if not line or warned_before(root, str(payload.get("session_id") or ""), line):
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": line}})
