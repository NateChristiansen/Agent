"""Recognise the owner's permitted read-only shell chains. Reject substitutions, redirects and
commands outside the fixed read-only list.

Command names alone do not establish read-only behavior: argument limits also apply. git can run
external diff helpers, so git, sed, find and rg stay outside this exemption. Permit only &&, ; and
| separators; substitutions, redirects, heredocs, backgrounding, ||, newlines and leading
environment assignments fall back to the one-command rule.
"""

from __future__ import annotations

import shlex

#: Commands with nothing that writes a file or runs another program, and the arguments that
#: would. A short-option cluster is checked letter by letter against `short`.
#: `rg` is not here: `--pre` runs a program, and an argument can reach it through quoting
#: (`$'--pre=…'`), a glob matching a file named `--pre=…`, or a config file named in the
#: environment. `grep` searches without any of that.
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
#: The only spellings of a command head: its bare name, or the system copy by exact path. A path
#: like `/usr/bin/../../tmp/x/ls` names another program.
HEADS = {name: name for name in ALLOWED} | {
    f"{prefix}/{name}": name for name in ALLOWED for prefix in ("/bin", "/usr/bin")
}

SEPARATORS = frozenset({"&&", ";", "|"})
#: Text that can start a substitution, an expansion (`$VAR`, `$'…'`, `${…}`), a redirection, a
#: heredoc or a second line.
FORBIDDEN_TEXT = ("`", "$", "<", ">", "\n")


def read_only_chain(cmd: str) -> bool:
    """Whether `cmd` is a chain the relaxed rule allows. False for anything doubtful."""
    if any(mark in cmd for mark in FORBIDDEN_TEXT):
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
            return False  # `||`, `&`, `|&` and the like
        else:
            segments[-1].append(token)
    return len(segments) > 1 and all(_allowed(segment) for segment in segments)


def _allowed(segment: list[str]) -> bool:
    if not segment:
        return False
    head = HEADS.get(segment[0])
    if head is None:
        return False
    limits = ALLOWED[head]
    long_bad = limits.get("long", frozenset())
    short_bad = limits.get("short", frozenset())
    # Every argument is checked, `--` included: an option's value can be `--` (`jq --arg key --
    # -f prog.jq`), so stopping there let `-f` through. A
    # file literally named like a refused option only costs a separate call.
    for arg in segment[1:]:
        if arg.startswith("--"):
            if arg.split("=", 1)[0] in long_bad:
                return False
        elif arg.startswith("-") and len(arg) > 1 and short_bad & set(arg[1:]):
            return False
    return True
