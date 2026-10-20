"""Respell safe Windows Codex PowerShell commands for POSIX worker permission rules. POSIX shlex
treats backslashes as escapes and does not know read cmdlet aliases. Normalize command paths, map
read-only cmdlets and quote every argument; deny either spelling before accepting an allow from
either.

Never respell on a host that is not Windows: a Claude worker on Windows runs Git Bash, where the
backslash really is an escape, so its POSIX parse is already the truth. Never respell text that
contains syntax outside _PLAIN: PowerShell expansions, smart quotes, line breaks, operators and
structures have no safe POSIX twin. Never map write cmdlets, whose option spellings could evade
denial rules.

Refuse remote or ambiguous path forms: colon-bound values, comma arrays, provider paths, device
paths, UNC or // hosts, mapped drives and named drives. Judge those on raw text rather than risk
turning a denial into an allow.
"""

from __future__ import annotations

import os
import re
import shlex
import string

#: Every character a body may hold to be respelled. PowerShell and a POSIX shell split these the
#: same way once the backslash is literal: words, straight quotes, wildcards and the pipe. No
#: comma: PowerShell reads `a,b` as an array, two operands in one word.
_PLAIN = frozenset(string.ascii_letters + string.digits + " _./\\:=+-'\"*?|")
#: In the body with its quotes removed (so `"\"\"\host"`-style concatenation cannot hide them):
#: a UNC or device prefix anywhere, `//`, and a provider qualifier.
_REMOTE_MARKS = ("\\\\", "//", "::")
#: A drive prefix that starts an operand, alone or colon-bound (`-Path:Z:\x`). Any name: besides
#: disk letters PowerShell has named drives (`Remote:` from New-PSDrive, which can sit on a UNC
#: share) and provider drives (`Env:`, `HKLM:`, `Cert:`). Only a single local disk letter keeps a
#: twin.
_DRIVE = re.compile(r"(?:^|[\s:=])([^\s:=\\/|]+):")


def drive_is_local(letter: str) -> bool:
    """`letter:` is a fixed disk or RAM disk on this machine (Win32 `GetDriveTypeW`), never a
    mapped network share. False off Windows, so an unknown drive fails closed."""
    if os.name != "nt":
        return False
    import ctypes

    kind = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
    return kind in (3, 6)  # DRIVE_FIXED, DRIVE_RAMDISK


def _remote(body: str) -> bool:
    bare = body.replace('"', "").replace("'", "")
    if any(mark in bare for mark in _REMOTE_MARKS):
        return True
    return not all(_local_drive(m.group(1)) for m in _DRIVE.finditer(bare))


def _local_drive(name: str) -> bool:
    """A one-letter disk drive that is local; every named or provider drive fails closed."""
    return len(name) == 1 and name.isascii() and name.isalpha() and drive_is_local(name)


#: Read cmdlets and their PowerShell aliases, by the POSIX command already granted on every
#: platform. None of these has a parameter that writes, runs code or (given `_PLAIN`) reaches
#: the network. Lower case: PowerShell command names are case-insensitive.
EQUIVALENTS = {
    "get-content": "cat",
    "gc": "cat",
    "type": "cat",
    "select-string": "grep",
    "sls": "grep",
    "get-childitem": "ls",
    "gci": "ls",
    "dir": "ls",
    "select-object": "head",
    "select": "head",
    "measure-object": "wc",
    "measure": "wc",
    "resolve-path": "realpath",
    "rvpa": "realpath",
    "get-filehash": "sha256sum",
    "get-location": "pwd",
    "gl": "pwd",
}


def powershell_host() -> bool:
    """Report whether Codex uses PowerShell here; a function lets tests substitute the host."""
    return os.name == "nt"


def words(body: str) -> list[str] | None:
    """`body` split as PowerShell splits it, or None when it holds anything outside `_PLAIN`
    or an unbalanced quote. The backslash is a literal, as in PowerShell; `|` is its own word."""
    if not body.strip() or not set(body) <= _PLAIN:
        return None
    lexer = shlex.shlex(body, posix=True, punctuation_chars="|")
    lexer.whitespace_split = True
    lexer.escape = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def respelled(command: str) -> str | None:
    """The POSIX twin of a PowerShell `command`, or None when there is none or it is the same
    text. Only on a PowerShell host."""
    if not powershell_host() or _remote(command):
        return None
    split = words(command)
    if not split:
        return None
    stages: list[list[str]] = [[]]
    for w in split:
        if w == "|":
            stages.append([])
        elif w.startswith("|"):
            return None  # `||` is a PowerShell 7 command list
        else:
            stages[-1].append(w)
    if not all(stages):
        return None
    out: list[str] = []
    for head, *args in stages:
        head = EQUIVALENTS.get(head.lower(), head.replace("\\", "/"))
        out.append(shlex.join([head, *args]))
    twin = " | ".join(out)
    return None if twin == command else twin
