# ruff: noqa
from __future__ import annotations
import re
import shlex
from pathlib import Path
from .paths import PROTECTED_NOTE


def _redirect_targets(command: str) -> list[str]:
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
            match = re.match("&\\d+\\b", command[i:])
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
    base = worktree.resolve()
    for raw in _redirect_targets(command):
        if re.fullmatch("&\\d+", raw):
            continue
        if not raw or raw.startswith("(") or any((char in raw for char in "$`*?[]{}\\")):
            return f"Worker output redirect has an unresolved target: {raw or '(missing)'}"
        target = raw.strip("\"'")
        if not target or '"' in target or "'" in target:
            return f"Worker output redirect has an unresolved target: {raw}"
        if reason := _outside(target, base, "output redirect", raw):
            return reason
    return _outside_copy(command, base)


def _outside(target: str, base: Path, what: str, raw: str) -> str | None:
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
_LAUNCHERS = {
    "xargs",
    "find",
    "parallel",
    "env",
    "sudo",
    "nohup",
    "time",
    "command",
    "exec",
    "nice",
    "timeout",
    "watch",
    "sh",
    "bash",
    "zsh",
    "eval",
}
_WRITER_RE = re.compile("(?:^|[\\s/;&|(])(?:cp|mv)(?:\\s|$)")
_CONTROL = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}


def segments(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for t in tokens:
        if t in _CONTROL:
            out.append([])
        else:
            out[-1].append(t)
    return [s for s in out if s]


def _copy_writes(args: list[str], name: str) -> list[str]:
    operands: list[str] = []
    target: str | None = None
    it = iter(args)
    end = False
    for a in it:
        if a and set(a) <= set("<>&|"):
            if operands and operands[-1].isdigit():
                operands.pop()
            break
        if end or a == "-" or (not a.startswith("-")):
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
            flag = min((i for i, ch in enumerate(a) if ch in "tS" and i > 0))
            rest = a[flag + 1 :] or next(it, "")
            if a[flag] == "t":
                target = rest
    writes = [target] if target is not None else operands[-1:] if len(operands) >= 2 else []
    if name == "mv":
        writes += operands if target is not None else operands[:-1]
    return writes


def newlines_as_separators(command: str) -> str:
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and (i + 1 < len(command)):
            out.append(command[i : i + 2])
            i += 2
            continue
        if ch in "\"'":
            quote = "" if quote == ch else quote or ch
        if ch == "\n" and (not quote):
            out.append(" ; ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _outside_copy(command: str, base: Path) -> str | None:
    try:
        lexer = shlex.shlex(newlines_as_separators(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = None
    if tokens is None:
        if re.search("\\b(cp|mv)\\b", command):
            return "Worker cp/mv command could not be parsed: unbalanced quoting"
        return None
    for seg in segments(tokens):
        words = list(seg)
        while words and re.fullmatch("[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words.pop(0)
        if not words:
            continue
        name = Path(words[0]).name
        if name in _LAUNCHERS and any((_WRITER_RE.search(w) for w in words[1:])):
            return f"Worker cp/mv is launched by {name}; its destination cannot be checked"
        if name not in _WRITERS:
            continue
        for raw in _copy_writes(words[1:], name):
            if not raw or any((ch in raw for ch in "$`*?[]{}\\")):
                return f"Worker {name} has an unresolved path: {raw or '(missing)'}"
            if reason := _outside(raw, base, name, raw):
                return reason
    return None
