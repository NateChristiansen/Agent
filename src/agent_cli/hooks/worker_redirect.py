"""Refuse worker shell writes beyond the dispatch worktree: output redirects, `cp` and `mv`."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .paths import PROTECTED_NOTE


def _redirect_targets(command: str) -> list[str]:
    """Return output redirect words, leaving dynamic words for the caller to refuse."""
    targets: list[str] = []
    i = 0
    quote = ""
    while i < len(command):
        char = command[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if char in "\"'":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            i += 1
            continue
        if char != ">" or quote:
            i += 1
            continue
        i += 1
        if i < len(command) and command[i] in ">|":
            i += 1
        while i < len(command) and command[i].isspace():
            i += 1
        if i < len(command) and command[i] == "&":
            match = re.match(r"&\d+\b", command[i:])
            if match:
                targets.append(match.group())
                i += len(match.group())
                continue
        start = i
        word_quote = ""
        while i < len(command):
            char = command[i]
            if char == "\\" and word_quote != "'":
                i += 2
                continue
            if char in "\"'":
                if not word_quote:
                    word_quote = char
                elif word_quote == char:
                    word_quote = ""
                i += 1
                continue
            if not word_quote and (char.isspace() or char in "<>|;&"):
                break
            i += 1
        targets.append(command[start:i])
    return targets


def outside_redirect(command: str, worktree: Path) -> str | None:
    reason = _outside_redirect(command, worktree)
    return f"{reason} {PROTECTED_NOTE}" if reason else None


def _outside_redirect(command: str, worktree: Path) -> str | None:
    """Describe the first unsafe worker write, or None. Check output redirects, cp and mv because
    allowed commands can write outside the worktree without a redirect. Refuse expansion and
    globbing when the destination cannot be known before execution. Resolve symlinks before
    comparing destinations.
    """
    base = worktree.resolve()
    for raw in _redirect_targets(command):
        # `2>&1` and `>&2` duplicate a file descriptor; they do not name a path.
        if re.fullmatch(r"&\d+", raw):
            continue
        if not raw or raw.startswith("(") or any(char in raw for char in "$`*?[]{}\\"):
            return f"Worker output redirect has an unresolved target: {raw or '(missing)'}"
        target = raw.strip("\"'")
        if not target or '"' in target or "'" in target:
            return f"Worker output redirect has an unresolved target: {raw}"
        if reason := _outside(target, base, "output redirect", raw):
            return reason
    return _outside_copy(command, base)


def _outside(target: str, base: Path, what: str, raw: str) -> str | None:
    """Why `target` is not a safe place to write, or None. `/dev/null` is the one sink allowed
    outside the worktree."""
    try:
        path = Path(target).expanduser()
        if path == Path("/dev/null"):
            return None
        resolved = (path if path.is_absolute() else base / path).resolve()
    except OSError, RuntimeError:
        return f"Worker {what} has an unresolved target: {raw}"
    if not resolved.is_relative_to(base):
        return f"Worker {what} escapes the dispatch worktree: {raw}"
    return None


_WRITERS = {"cp", "mv"}
#: Commands that run another command from their arguments. A cp/mv anywhere in their words
#: fails closed, even where it would have been safe (`time cp a b`): the cost is a retry.
_LAUNCHERS = {
    "xargs", "find", "parallel", "env", "sudo", "nohup", "time", "command", "exec", "nice",
    "timeout", "watch", "sh", "bash", "zsh", "eval",
}  # fmt: skip
#: cp or mv as a whole word, including inside an `sh -c '...'` string or after a path.
_WRITER_RE = re.compile(r"(?:^|[\s/;&|(])(?:cp|mv)(?:\s|$)")
_CONTROL = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}


def segments(tokens: list[str]) -> list[list[str]]:
    """Simple commands, split at control operators."""
    out: list[list[str]] = [[]]
    for t in tokens:
        if t in _CONTROL:
            out.append([])
        else:
            out[-1].append(t)
    return [s for s in out if s]


def _copy_writes(args: list[str], name: str) -> list[str]:
    """The paths a `cp` or `mv` call writes: its destination, and for `mv` its sources too,
    since a move deletes them. Redirect words end the argument list; `_redirect_targets` owns
    them."""
    operands: list[str] = []
    target: str | None = None
    it = iter(args)
    end = False
    for a in it:
        if a and set(a) <= set("<>&|"):
            if operands and operands[-1].isdigit():
                operands.pop()  # the fd number of `2>`, split off by the lexer
            break
        if end or a == "-" or not a.startswith("-"):
            operands.append(a)
        elif a == "--":
            end = True
        elif a.startswith("--target-directory="):
            target = a.split("=", 1)[1]
        elif a in ("--target-directory", "-t"):
            target = next(it, "")
        elif a in ("--suffix", "-S"):
            next(it, "")
        elif not a.startswith("--") and ("t" in a[1:] or "S" in a[1:]):
            # A short-flag cluster such as `-rt DIR` or `-rtDIR`.
            flag = min(i for i, ch in enumerate(a) if ch in "tS" and i > 0)
            rest = a[flag + 1 :] or next(it, "")
            if a[flag] == "t":
                target = rest
    writes = [target] if target is not None else operands[-1:] if len(operands) >= 2 else []
    if name == "mv":
        writes += operands if target is not None else operands[:-1]
    return writes


def newlines_as_separators(command: str) -> str:
    """Turn each unquoted, unescaped newline into `; `. shlex reads a newline as whitespace,
    so `echo ok` then a newline then `cp evil /tmp/x` lexed as one echo command."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and i + 1 < len(command):
            out.append(command[i : i + 2])
            i += 2
            continue
        if ch in "\"'":
            quote = "" if quote == ch else (quote or ch)
        if ch == "\n" and not quote:
            out.append(" ; ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _outside_copy(command: str, base: Path) -> str | None:
    """Why a `cp` or `mv` in `command` writes outside the worktree, or None."""
    try:
        lexer = shlex.shlex(newlines_as_separators(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = None
    if tokens is None:
        if re.search(r"\b(cp|mv)\b", command):
            return "Worker cp/mv command could not be parsed: unbalanced quoting"
        return None
    for seg in segments(tokens):
        words = list(seg)
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words.pop(0)
        if not words:
            continue
        name = Path(words[0]).name
        if name in _LAUNCHERS and any(_WRITER_RE.search(w) for w in words[1:]):
            # `xargs -0 cp`, `find -exec env cp`, `sudo -u x mv`, `sh -c 'cp ...'`: the launcher's
            # own options sit between it and cp/mv, so the destination cannot be read reliably.
            return f"Worker cp/mv is launched by {name}; its destination cannot be checked"
        if name not in _WRITERS:
            continue
        for raw in _copy_writes(words[1:], name):
            if not raw or any(ch in raw for ch in "$`*?[]{}\\"):
                return f"Worker {name} has an unresolved path: {raw or '(missing)'}"
            if reason := _outside(raw, base, name, raw):
                return reason
    return None
