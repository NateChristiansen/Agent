# ruff: noqa
from __future__ import annotations
import json
import ntpath
import os
import posixpath
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any
from .. import core, sessions
from ..records.narrow import as_dict

PROTECTED_PATH_RE = re.compile(
    "(^|/)(STATUS\\.md|Dashboard\\.md|Index\\.md|adrs/README\\.md|bases/[^\\s]+|features/[^/\\s]+/(log\\.md|handoffs(/[^\\s]*)?|records(/[^\\s]*)?)|initiatives/[^\\s]*/?(log\\.md|handoffs(/[^\\s]*)?|records(/[^\\s]*)?)|"
    + core.agent_path_pattern()
    + "|journal/[^/\\s]*\\.collect\\.[^\\s]*)$",
    re.IGNORECASE,
)
VAULT_GENERATED_DENY = (
    "Edit(/STATUS.md)",
    "Edit(/Dashboard.md)",
    "Edit(/Index.md)",
    "Edit(/bases/**)",
    "Edit(/adrs/README.md)",
    "Edit(/features/*/log.md)",
    "Edit(/features/*/handoffs/**)",
    "Edit(/features/*/records/**)",
    "Edit(/initiatives/**/log.md)",
    "Edit(/initiatives/**/handoffs/**)",
    "Edit(/initiatives/**/records/**)",
    "Edit(/records/**)",
)
NATIVE_GENERATED_DENY = (*VAULT_GENERATED_DENY, *core.agent_deny_patterns(), "Edit(/journal/*.collect.*)")
HEREDOC_RE = re.compile("<<-?\\s*(['\\\"]?)([A-Za-z_][A-Za-z0-9_]*)\\1")
PRODUCT_CODE_REASON = (
    "you are the manager; you do not write product code. Route this through /dispatch instead."
)


def decision(decision: str, reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
    )


def deny(reason: str) -> str:
    return decision("deny", reason)


def ask(reason: str) -> str:
    return decision("ask", reason)


PROTECTED_NOTE = "(protected core hook: it cannot be switched off)"


def removable_note(name: str) -> str:
    return f"(removable: set `hooks: {{{name}: false}}` in agent.yaml or agent.local.yaml)"


NOTED_BLOCK_EVENTS = frozenset({"Stop", "SubagentStop"})


def add_hook_note(out: str, name: str, removable: bool, event: str) -> str:
    if not out:
        return out
    data = as_dict(json.loads(out))
    note = removable_note(name) if removable else PROTECTED_NOTE
    specific = as_dict(data.get("hookSpecificOutput"))
    if specific.get("permissionDecision") == "deny":
        target = specific
        key = "permissionDecisionReason"
    elif data.get("decision") == "block" and event in NOTED_BLOCK_EVENTS:
        target = data
        key = "reason"
    else:
        return out
    target[key] = f"{target.get(key) or ''} {note}".strip()
    return json.dumps(data)


def slashes(path: str) -> str:
    return path.replace("\\", "/")


def canon(path: Path) -> str:
    p = str(path.resolve())
    return p.lower() if sys.platform in ("darwin", "win32") else p


def relative(root: Path, file_path: str) -> str:
    p = Path(file_path)
    if not p.is_absolute():
        p = root / p
    path_canon = canon(p)
    root_canon = canon(root)
    if path_canon == root_canon:
        return ""
    if path_canon.startswith(root_canon + os.sep):
        return slashes(path_canon[len(root_canon) + 1 :])
    return slashes(path_canon)


TMP_PREFIXES = tuple(
    (
        slashes(canon(Path(p)))
        for p in {tempfile.gettempdir(), "/tmp", "/private/tmp", os.environ.get("TMPDIR", "/tmp")}
    )
)


def under_registered_repo(root: Path, file_path: str) -> bool:
    try:
        from .. import repos as repos_mod

        registry = repos_mod.load_registry(root)
    except Exception:
        return False
    p = Path(file_path)
    if not p.is_absolute():
        p = root / p
    target = canon(p)
    root_canon = canon(root)
    for repo in registry.repos:
        path = repo.abs_path(root)
        bases = [path] if canon(path) != root_canon else []
        for base in (*bases, repo.abs_worktrees(root)):
            if base is None:
                continue
            b = canon(base)
            if target == b or target.startswith(b + os.sep):
                return True
    return False


def session_feature(root: Path, session_id: str) -> dict[str, Any] | None:
    try:
        record = sessions.get(root, session_id)
    except Exception:
        record = None
    feature = (record or {}).get("feature")
    if not feature:
        return None
    return {"id": feature.get("id"), "slug": feature.get("slug")}


OPERATE_VERBS = ("build", "test", "restore", "lint", "install", "--version", "--info")
PACKAGE_HEADS = ("dotnet", "pnpm", "npm", "yarn")


def _script_verb(toks: list[str]) -> str | None:
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in ("--dir", "-C", "--filter", "-F"):
            i += 2
            continue
        if t.startswith(("--dir=", "--filter=", "-C=")):
            i += 1
            continue
        return t
    return None


def _operates_not_writes(toks: list[str], bases: list[str]) -> bool:
    if bases[0] == "time" and len(toks) >= 2:
        return _operates_not_writes(toks[1:], [os.path.basename(toks[1])])
    if bases[0] not in PACKAGE_HEADS:
        return False
    verb = _script_verb(toks)
    if verb is None:
        return False
    return verb in OPERATE_VERBS or verb.startswith(("dev", "start", "setup:"))


def check_path(rel: str) -> str:
    parts = rel.split("/")
    if parts and parts[0] == "repos":
        return deny(PRODUCT_CODE_REASON)
    return ""


READ_ONLY_HEADS = {
    "git",
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "find",
    "wc",
    "stat",
    "file",
    "diff",
    "shasum",
    "sha256sum",
    "md5",
    "realpath",
    "tree",
    "less",
    "more",
    "obsidian",
    "bin/agent",
}
STREAM_EDITORS = {"sed", "perl", "awk"}
DELETE_FLAG_RE = re.compile("^-[A-Za-z]*[rRf][A-Za-z]*$|^--(recursive|force)$")
IN_PLACE_FLAG_RE = re.compile("^-[A-Za-z]*i[A-Za-z]*$|^--in-place")
PROTECTED_DIRS = {"features", "adrs", "bases", core.agent_dir_name(), "journal", "harness", "src", "bin"}
SHELL_WRITE_REASON = "shell writes to generated files are not allowed. Use the agent MCP tools instead: log, status_render, task_add / task_done, feature_use."


def drop_heredoc_bodies(cmd: str) -> str:
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m and (not _inside_quotes(line, m.start())):
            tag = m.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1
            continue
        i += 1
    return "\n".join(out)


def _inside_quotes(line: str, pos: int) -> bool:
    sq = line[:pos].count("'") % 2 == 1
    dq = line[:pos].count('"') % 2 == 1
    return sq or dq


ENV_ASSIGNMENT_RE = re.compile("^[A-Za-z_][A-Za-z0-9_]*=")


def _op_tokens(cmd: str) -> list[str]:
    text = drop_heredoc_bodies(cmd).replace("\\\n", " ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=">|<&;")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return [t for t in lexer if t.strip()]
    except ValueError:
        return text.split()


PROTECTED_DIR_RE = re.compile(
    "(^|/)(" + "|".join((re.escape(d) for d in PROTECTED_DIRS)) + ")(/|$)", re.IGNORECASE
)


def _protected(tok: str, dirs: bool = False) -> bool:
    tok = slashes(tok.split("=", 1)[1] if tok.startswith(("of=", "file=")) else tok)
    spellings = {tok, posixpath.normpath(tok)} if tok else {tok}
    if any((PROTECTED_PATH_RE.search(s) for s in spellings)):
        return True
    return dirs and any((PROTECTED_DIR_RE.search(s) for s in spellings))


def _writes_protected(root: Path, tok: str) -> bool:
    if _protected(tok):
        return True
    target = slashes(tok.split("=", 1)[1] if tok.startswith(("of=", "file=")) else tok)
    if not target or target.startswith("-"):
        return False
    try:
        from .notes_paths import protected_write

        return protected_write(root, target)
    except Exception:
        return False


def check_command(root: Path, cmd: str) -> str:
    for reading in shell_readings(cmd):
        denial = _check_tokens(root, _op_tokens(reading))
        if denial:
            return denial
    return ""


def shell_readings(cmd: str) -> list[str]:
    readings = [cmd]
    if sys.platform == "win32" and "\\" in cmd:
        readings.append(slashes(cmd))
    return readings


def _check_tokens(root: Path, toks: list[str]) -> str:
    while toks and ENV_ASSIGNMENT_RE.match(toks[0]):
        toks = toks[1:]
    if not toks:
        return ""
    bases = [os.path.basename(t) for t in toks]
    if "rm" in bases:
        i = bases.index("rm")
        rest = toks[i + 1 :]
        if any((DELETE_FLAG_RE.match(t) for t in rest)) and any((_protected(t, dirs=True) for t in rest)):
            return deny(
                "recursive or forced delete inside the harness is not allowed. Close features with the feature_close tool; supersede ADRs with the adr_new tool and `supersedes`."
            )
    if "find" in bases and any((t in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for t in toks)):
        return deny(
            "find -delete/-exec is not allowed. List the matches with `find … -print`, then run a read-only command on those paths (`wc -l a b`, `shasum a b`), or act one path at a time."
        )
    if (
        ("sed" in bases or "perl" in bases)
        and any((IN_PLACE_FLAG_RE.match(t) for t in toks))
        and any((_writes_protected(root, t) for t in toks))
    ):
        return deny(SHELL_WRITE_REASON)
    if any((b in ("truncate", "dd", "install", "touch") for b in bases)) and any(
        (_writes_protected(root, t) for t in toks[1:])
    ):
        return deny(SHELL_WRITE_REASON)
    if bases[0] in ("cp", "mv", "ln") and len(toks) >= 2 and _writes_protected(root, toks[-1]):
        return deny(SHELL_WRITE_REASON)
    for i, t in enumerate(toks):
        if t in (">", ">>", ">|") and i + 1 < len(toks) and _writes_protected(root, toks[i + 1]):
            return deny(SHELL_WRITE_REASON)
        if os.path.basename(t) == "tee":
            for target in toks[i + 1 :]:
                if target in ("|", "&", ";", ">", ">>"):
                    break
                if target.startswith("-"):
                    continue
                if _writes_protected(root, target):
                    return deny(SHELL_WRITE_REASON)
    reads_only = (
        bases[0] in READ_ONLY_HEADS
        and (not _writing_option(bases[0], toks))
        or (bases[0] in STREAM_EDITORS and (not any((IN_PLACE_FLAG_RE.match(t) for t in toks))))
    )
    if not reads_only and toks[0] != "bin/agent" and (not _operates_not_writes(toks, bases)):
        for t in toks[1:]:
            if _outside_spelling(t) and under_registered_repo(root, t):
                return deny(PRODUCT_CODE_REASON)
    return ""


def _outside_spelling(tok: str) -> bool:
    if tok.startswith(("/", "../")):
        return True
    return sys.platform == "win32" and (ntpath.isabs(tok) or tok.startswith("..\\"))


def _writing_option(head: str, toks: list[str]) -> bool:
    return (
        head == "file"
        and any((t.startswith("-") and "C" in t for t in toks[1:] if not t.startswith("--")))
        or (head == "tree" and any((t == "-o" or t.startswith("-o") for t in toks[1:])))
    )


def shell_tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()
