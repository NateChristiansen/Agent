# ruff: noqa
from __future__ import annotations
import re
import shlex

_INERT = {"stat", "realpath", "basename", "dirname", "shasum", "sha256sum", "sha1sum", "md5", "md5sum"}
_JQ_VALUED = {"--arg", "--argjson", "--rawfile", "--slurpfile", "--indent", "-f", "--from-file"}
_JQ_FLAGS = re.compile(
    "^-[rcejsnSaCMR]+$|^--(?:raw-output|compact-output|exit-status|slurp|null-input|sort-keys|tab|join-output|ascii-output|seq|stream|raw-input)$"
)
_ADDR = "(?:\\d+|\\$|/(?:[^/\\\\]|\\\\.)*/)"
_SED_PRINT = re.compile(f"^{_ADDR}(?:,{_ADDR})?p$")
_OPERATOR = re.compile("[;&|<>()]+")
_FILTERS = {"cat", "head", "tail", "grep", "rg", "wc", "cut", "tr", "nl", "sort", "uniq"}
_UNSAFE_OPTION = {"rg": re.compile("^(?:--pre|--hostname-bin|--search-zip)|^-[A-Za-z]*z")}
_SAFE_OPTIONS = {
    "sort": (re.compile("^-[bdfghiMnRrsuVz]+$"), {"-k", "-t"}),
    "uniq": (re.compile("^-[cdiuz]+$"), {"-f", "-s", "-w"}),
}
_NL_OPTION = re.compile("^-(?:[bhfvinwsdlp][^ ]*|-[a-z-]+(?:=.*)?)$")


def reason(command: str) -> str:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return ""
    if any(("`" in w or "$(" in w for w in words)):
        return ""
    stages: list[list[str]] = [[]]
    for w in words:
        if w == "|":
            stages.append([])
        elif _OPERATOR.fullmatch(w):
            return ""
        else:
            stages[-1].append(w)
    if not all(stages):
        return ""
    reasons = [_stage(stage) for stage in stages]
    if not all(reasons):
        return ""
    if len(stages) == 1:
        return reasons[0]
    return "read-only pipeline: " + " | ".join((f"`{stage[0]}`" for stage in stages))


def _stage(words: list[str]) -> str:
    head, args = (words[0], words[1:])
    if head in _INERT:
        return f"read-only `{head}`"
    if head == "pwd" and set(args) <= {"-P", "-L"}:
        return "read-only `pwd`"
    if head == "jq" and _jq_ok(args):
        return "read-only `jq`"
    if head == "sed" and _sed_ok(args):
        return "read-only `sed -n` print"
    if head == "nl" and all((_NL_OPTION.match(a) or not a.startswith("-") for a in args)):
        return "read-only `nl`"
    if head in _FILTERS and _filter_ok(head, args):
        return f"read-only `{head}`"
    return ""


def _filter_ok(head: str, args: list[str]) -> bool:
    unsafe = _UNSAFE_OPTION.get(head)
    if unsafe is not None and any((unsafe.match(a) for a in args)):
        return False
    if head not in _SAFE_OPTIONS:
        return True
    flags, valued = _SAFE_OPTIONS[head]
    operands: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in valued:
            i += 2
            continue
        if a.startswith("-") and a != "-":
            if not flags.match(a):
                return False
        else:
            operands.append(a)
        i += 1
    return head != "uniq" or len(operands) <= 1


def _jq_ok(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a in _JQ_VALUED:
            i += 2 if a not in ("--rawfile", "--slurpfile", "--arg", "--argjson") else 3
            continue
        if a.startswith("-") and a != "-" and (not _JQ_FLAGS.match(a)):
            return False
        i += 1
    return True


def _sed_ok(args: list[str]) -> bool:
    flags = [a for a in args if a.startswith("-")]
    rest = [a for a in args if not a.startswith("-")]
    if "-n" not in flags or set(flags) - {"-n", "-E", "-r"}:
        return False
    return bool(rest) and bool(_SED_PRINT.match(rest[0]))
