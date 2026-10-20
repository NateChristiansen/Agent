"""Recognise worker commands that cannot write files, run other code or reach the network. Validate
options as well as names: file -C compiles output and tree -o writes files, so neither command
belongs here. Explicit worker grants are a separate permission path.

A pipeline is read-only only when every stage qualifies. Redirects, command lists and substitution
exclude it. Writers and code runners such as tee, xargs, sh, awk, sort -o and rg --pre cannot
qualify. Use this same class for both providers and preflight.
"""

from __future__ import annotations

import re
import shlex

from ..quote_mask import mask_quotes

#: Commands that have no option able to write, execute or reach the network; any arguments.
_INERT = {"stat", "realpath", "basename", "dirname", "shasum", "sha256sum", "sha1sum", "md5", "md5sum"}
#: jq reads its inputs and prints; these are the options that take a value.
_JQ_VALUED = {"--arg", "--argjson", "--rawfile", "--slurpfile", "--indent", "-f", "--from-file"}
_JQ_FLAGS = re.compile(
    r"^-[rcejsnSaCMR]+$|^--(?:raw-output|compact-output|exit-status|slurp|null-input|sort-keys|tab|join-output|ascii-output|seq|stream|raw-input)$"
)
#: `sed -n` with exactly one print-only script: a line number, `$` or `/regex/`, optionally a range.
_ADDR = r"(?:\d+|\$|/(?:[^/\\]|\\.)*/)"
_SED_PRINT = re.compile(rf"^{_ADDR}(?:,{_ADDR})?p$")
_OPERATOR = re.compile(r"[;&|<>()]+")
#: Text readers and filters with no option that writes, runs code or reaches the network, except
#: the options named in `_UNSAFE_OPTION`; any other arguments.
_FILTERS = {"cat", "head", "tail", "grep", "rg", "wc", "cut", "tr", "nl", "sort", "uniq"}
#: Per filter, the options that write a file or run another program (`rg --pre`, `rg -z`
#: decompressors), long form or inside a short-option cluster.
_UNSAFE_OPTION = {
    "rg": re.compile(r"^(?:--pre|--hostname-bin|--search-zip)|^-[A-Za-z]*z"),
}
#: `sort` and `uniq` accept abbreviated long options (`--out=` is `--output=`) and `uniq` writes
#: its second operand, so for them only these options are read stages.
_SAFE_OPTIONS = {
    "sort": (re.compile(r"^-[bdfghiMnRrsuVz]+$"), {"-k", "-t"}),
    "uniq": (re.compile(r"^-[cdiuz]+$"), {"-f", "-s", "-w"}),
}
#: `nl` options: numbering style, width, separator and the like; none takes a file to write.
_NL_OPTION = re.compile(r"^-(?:[bhfvinwsdlp][^ ]*|-[a-z-]+(?:=.*)?)$")
# A version query names one bare executable, with no path, wrapper or preceding code arguments.
_EXECUTABLE = re.compile(r"[A-Za-z0-9_.:@+-]+")


def reason(command: str) -> str:
    """Why `command` is in the read-only class, or "" when it is not. Any redirection, command
    list or substitution takes it out; a pipe keeps it in only when every stage reads. Quoted text
    is never read as an operator."""
    if re.search(r"[\n\r]", mask_quotes(command)):
        return ""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return ""
    if any("`" in w or "$(" in w for w in words):
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
    return "read-only pipeline: " + " | ".join(f"`{stage[0]}`" for stage in stages)


def _stage(words: list[str]) -> str:
    head, args = words[0], words[1:]
    if (
        args == ["--version"]
        and head not in {".", "source"}
        and not head.startswith("-")
        and _EXECUTABLE.fullmatch(head)
    ):
        return "read-only version query"
    if words == ["agent", "--help"]:
        return "read-only `agent --help`"
    if head == "ls":
        return "read-only `ls`"
    if head in _INERT:
        return f"read-only `{head}`"
    if head == "pwd" and set(args) <= {"-P", "-L"}:
        return "read-only `pwd`"
    if head == "jq" and _jq_ok(args):
        return "read-only `jq`"
    if head == "sed" and _sed_ok(args):
        return "read-only `sed -n` print"
    if head == "nl" and all(_NL_OPTION.match(a) or not a.startswith("-") for a in args):
        return "read-only `nl`"
    if head in _FILTERS and _filter_ok(head, args):
        return f"read-only `{head}`"
    return ""


def _filter_ok(head: str, args: list[str]) -> bool:
    unsafe = _UNSAFE_OPTION.get(head)
    if unsafe is not None and any(unsafe.match(a) for a in args):
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
                return False  # any long option, `--`, or a flag outside the list
        else:
            operands.append(a)
        i += 1
    # `uniq in out` writes `out`; a reading stage takes its input from the pipe or one file.
    return head != "uniq" or len(operands) <= 1


def _jq_ok(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a in _JQ_VALUED:
            i += 2 if a not in ("--rawfile", "--slurpfile", "--arg", "--argjson") else 3
            continue
        if a.startswith("-") and a != "-" and not _JQ_FLAGS.match(a):
            return False
        i += 1
    return True


def _sed_ok(args: list[str]) -> bool:
    flags = [a for a in args if a.startswith("-")]
    rest = [a for a in args if not a.startswith("-")]
    if "-n" not in flags or set(flags) - {"-n", "-E", "-r"}:
        return False
    return bool(rest) and bool(_SED_PRINT.match(rest[0]))
