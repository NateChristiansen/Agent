# ruff: noqa
from __future__ import annotations
import shlex

ALLOWED: dict[str, dict[str, frozenset[str]]] = {
    "ls": {},
    "cat": {},
    "head": {},
    "tail": {},
    "wc": {},
    "echo": {},
    "grep": {},
    "jq": {"long": frozenset({"--from-file"}), "short": frozenset({"f"})},
}
HEADS = {name: name for name in ALLOWED} | {
    f"{prefix}/{name}": name for name in ALLOWED for prefix in ("/bin", "/usr/bin")
}
SEPARATORS = frozenset({"&&", ";", "|"})
FORBIDDEN_TEXT = ("`", "$", "<", ">", "\n")


def read_only_chain(cmd: str) -> bool:
    if any((mark in cmd for mark in FORBIDDEN_TEXT)):
        return False
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            segments.append([])
        elif token and set(token) <= set(";&|"):
            return False
        else:
            segments[-1].append(token)
    return len(segments) > 1 and all((_allowed(segment) for segment in segments))


def _allowed(segment: list[str]) -> bool:
    if not segment:
        return False
    head = HEADS.get(segment[0])
    if head is None:
        return False
    limits = ALLOWED[head]
    long_bad = limits.get("long", frozenset())
    short_bad = limits.get("short", frozenset())
    for arg in segment[1:]:
        if arg.startswith("--"):
            if arg.split("=", 1)[0] in long_bad:
                return False
        elif arg.startswith("-") and len(arg) > 1 and short_bad & set(arg[1:]):
            return False
    return True
