"""Path and command classification: the protected-path regex, the deny lists, and the
Edit/Write and Bash halves of `protect-generated`'s path checks (`check_path`,
`check_command`). Also the generic PreToolUse decision builders (`decision`/`deny`/`ask`)
and a handful of small helpers (`canon`, `relative`, `shell_tokens`, `drop_heredoc_bodies`,
`session_feature`) shared by the hook modules that build on top of this one. This module has
no dependency on the others under `hooks/`; everything else in the package imports from it.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shlex
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .. import core, sessions
from ..records.narrow import as_dict

# Files the CLI owns. Same set for Edit/Write paths and for Bash write detection. Bash writes
# (redirects, tee, cp, mv, rm) can still reach any of these. This stays the source of truth
# for `check_command`, even though the Edit/Write half below is mostly native now. The `.agent`
# branch comes from `core.agent_path_pattern()`. One registry (`core.AGENT_SUBTREES`) feeds
# this regex, the deny list below, and doctor's settings.json check.
PROTECTED_PATH_RE = re.compile(
    r"(^|/)(STATUS\.md|Dashboard\.md|Index\.md|adrs/README\.md|bases/[^\s]+|"
    r"features/[^/\s]+/(log\.md|handoffs(/[^\s]*)?|records(/[^\s]*)?)|"
    r"initiatives/[^\s]*/?(log\.md|handoffs(/[^\s]*)?|records(/[^\s]*)?)|"
    + core.agent_path_pattern()
    + r"|journal/[^/\s]*\.collect\.[^\s]*)$",
    re.IGNORECASE,
)
# Native Edit rules cover Write, MultiEdit and NotebookEdit too. Keep their generated-path patterns
# as shared constants for NATIVE_GENERATED_DENY and notes_paths.is_generated; tests pin agreement
# with settings.
VAULT_GENERATED_DENY = (
    "Edit(/STATUS.md)",
    "Edit(/Dashboard.md)",
    "Edit(/Index.md)",
    "Edit(/bases/**)",
    "Edit(/adrs/README.md)",
    "Edit(/features/*/log.md)",
    "Edit(/features/*/handoffs/**)",
    # Keep generated_denial's protected notes paths identical to the native settings deny list.
    "Edit(/features/*/records/**)",
    "Edit(/initiatives/**/log.md)",
    "Edit(/initiatives/**/handoffs/**)",
    "Edit(/initiatives/**/records/**)",
    "Edit(/records/**)",
)
NATIVE_GENERATED_DENY = (
    *VAULT_GENERATED_DENY,
    *core.agent_deny_patterns(),
    "Edit(/journal/*.collect.*)",
)

HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

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
    """`out` with the switch or protected note appended to a PreToolUse deny, or to a block
    from a Stop or SubagentStop hook (`event` is the payload's `hook_event_name`). A
    UserPromptSubmit block drops a stale wake and no user acts on it, so it and other JSON
    output come back unchanged; output that is not JSON raises, and the caller keeps the
    original."""
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


# ------------------------------------------------------------------ protect-generated: paths


def slashes(path: str) -> str:
    """A path spelled with forward slashes, for matching only; never for opening a file.

    Every path regex and `split("/")` in the guards is written for `/`. Windows hands them
    `features\\030\\log.md`, and `protect-generated` let such a patch through. A backslash in a real POSIX file name is legal but never names a harness
    file, so treating it as a separator on every OS only ever adds a denial."""
    return path.replace("\\", "/")


def canon(path: Path) -> str:
    """Resolved, case-folded on case-insensitive filesystems (macOS APFS and NTFS by default).

    os.path.normcase is a no-op on posix. Fold explicitly."""
    p = str(path.resolve())
    return p.lower() if sys.platform in ("darwin", "win32") else p


def relative(root: Path, file_path: str) -> str:
    """Path relative to root (normalized, case-folded, forward slashes), or the absolute
    canonical path when it lies outside root. Forward slashes on every OS: the callers split on
    `/` and match `/`-only patterns."""
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


# Slashed like `relative`'s output, which callers match these against.
TMP_PREFIXES = tuple(
    slashes(canon(Path(p)))
    for p in {tempfile.gettempdir(), "/tmp", "/private/tmp", os.environ.get("TMPDIR", "/tmp")}  # noqa: S108 — checking for the temp dir is the point
)


def under_registered_repo(root: Path, file_path: str) -> bool:
    try:
        from .. import repos as repos_mod

        registry = repos_mod.load_registry(root)
    except Exception:  # noqa: BLE001
        return False
    p = Path(file_path)
    if not p.is_absolute():
        p = root / p
    target = canon(p)
    root_canon = canon(root)
    for repo in registry.repos:
        path = repo.abs_path(root)
        # The harness registers itself with `path: .`. Its
        # own checkout is the manager's, not product code. Only `path` is exempt: a worktrees_dir
        # at the root is a misconfiguration, and refusing there fails closed.
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
    except Exception:  # noqa: BLE001
        record = None
    feature = (record or {}).get("feature")
    if not feature:
        return None
    return {"id": feature.get("id"), "slug": feature.get("slug")}


OPERATE_VERBS = ("build", "test", "restore", "lint", "install", "--version", "--info")
PACKAGE_HEADS = ("dotnet", "uv", "pnpm", "npm", "yarn")


def _script_verb(toks: list[str]) -> str | None:
    """The verb in `pnpm [--dir P | -C P | --filter X | --filter=X]... <verb>`."""
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
    """A package-manager invocation that builds, tests, installs or runs a script.

    These are the commands a person stands a repo up with. Contrast with one that edits files
    (`exec`, `pnpm patch`, or any non-package-manager write)."""
    if bases[0] == "time" and len(toks) >= 2:
        return _operates_not_writes(toks[1:], [os.path.basename(toks[1])])
    if bases[0] not in PACKAGE_HEADS:
        return False
    verb = _script_verb(toks)
    if verb is None:
        return False
    return verb in OPERATE_VERBS or verb.startswith(("dev", "start", "setup:"))


def check_path(rel: str) -> str:
    """Classify Edit/Write targets not already handled by registered-repo checks. Native settings deny
    CLI-owned generated paths and ask on guardrail/owner files before this hook runs. Keep the
    literal repos/ guard here because it has no native equivalent.
    """
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
DELETE_FLAG_RE = re.compile(r"^-[A-Za-z]*[rRf][A-Za-z]*$|^--(recursive|force)$")
IN_PLACE_FLAG_RE = re.compile(r"^-[A-Za-z]*i[A-Za-z]*$|^--in-place")
PROTECTED_DIRS = {"features", "adrs", "bases", core.agent_dir_name(), "journal", "harness", "src", "bin"}
SHELL_WRITE_REASON = (
    "shell writes to generated files are not allowed. Use the agent MCP tools instead: log, "
    "status_render, task_add / task_done, feature_use."
)


def drop_heredoc_bodies(cmd: str) -> str:
    """Remove heredoc bodies (between the `<<TAG` line and the `TAG` terminator line).

    The body's contents are not scanned. Everything after the terminator still is."""
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m and not _inside_quotes(line, m.start()):
            tag = m.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1  # skip the terminator itself
            continue
        i += 1
    return "\n".join(out)


def _inside_quotes(line: str, pos: int) -> bool:
    sq = line[:pos].count("'") % 2 == 1
    dq = line[:pos].count('"') % 2 == 1
    return sq or dq


ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _op_tokens(cmd: str) -> list[str]:
    """Shell tokens with quotes removed and the operators `> >> >| < | & ;` split out.

    A `>` inside a quoted argument is text. A bare `>` is a redirect."""
    text = drop_heredoc_bodies(cmd).replace("\\\n", " ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=">|<&;")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return [t for t in lexer if t.strip()]
    except ValueError:
        return text.split()


PROTECTED_DIR_RE = re.compile(
    r"(^|/)(" + "|".join(re.escape(d) for d in PROTECTED_DIRS) + r")(/|$)", re.IGNORECASE
)


def _protected(tok: str, dirs: bool = False) -> bool:
    """A token that names a CLI-owned file, in any path spelling (relative, ./, absolute,
    or $PWD-prefixed). With dirs=True a harness directory itself also counts. That is the
    right test for recursive deletes, but not for writes: plan.md, design notes, and ADR
    bodies under those directories are hand-editable."""
    tok = slashes(tok.split("=", 1)[1] if tok.startswith(("of=", "file=")) else tok)
    # `features/x/../x/log.md` and `journal/./x.collect.json` name the same files.
    spellings = {tok, posixpath.normpath(tok)} if tok else {tok}
    if any(PROTECTED_PATH_RE.search(s) for s in spellings):
        return True
    return dirs and any(PROTECTED_DIR_RE.search(s) for s in spellings)


def _writes_protected(root: Path, tok: str) -> bool:
    """A write target that is CLI-owned by its spelling, or by where it lands in the vault: the
    root-anchored rules (`records/**`) and notes paths reached through a link."""
    if _protected(tok):
        return True
    target = slashes(tok.split("=", 1)[1] if tok.startswith(("of=", "file=")) else tok)
    if not target or target.startswith("-"):
        return False
    try:
        from .notes_paths import protected_write

        return protected_write(root, target)
    except Exception:  # noqa: BLE001 — an unresolvable spelling is judged by the regex alone
        return False


SCRATCH_REL = core.agent_path(PurePosixPath(), "scratch").as_posix()


def activity_guard(root: Path, payload: dict[str, Any], raw: str) -> str:
    """Status-only manager edit check. This hook is not a sandbox for arbitrary writers."""
    try:
        from .. import activity_gate
        from .notes_paths import _expand_pwd, notes_logical  # pyright: ignore[reportPrivateUsage]
        from .worktree_guard import _gitdir_of, manager_worktree  # pyright: ignore[reportPrivateUsage]

        session_id = str(payload.get("session_id") or "") or None
        record = sessions.get(root, session_id) if session_id else {}
        if (
            os.environ.get("AGENT_DISPATCH_ID")
            or as_dict(record).get("dispatch_id")
            or payload.get("agent_id")
            or payload.get("agent_type")
        ):
            return ""
        raw = _expand_pwd(root, raw)
        target = (root / raw).resolve()
        written = manager_worktree(root, str(target))
        branch = None
        if written is not None and written[1] is not None:
            rel = written[1]
            checkout = target.parents[len(Path(rel).parts) - 1]
            admin = _gitdir_of(checkout / ".git")
            if admin is not None:
                head = (admin / "HEAD").read_text(encoding="utf-8").strip()
                branch = (
                    head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else None
                )
            if rel in ("features", SCRATCH_REL) or rel.startswith(("features/", SCRATCH_REL + "/")):
                return ""
        else:
            rel = relative(root, raw)
            if os.path.isabs(rel):
                rel = notes_logical(root, rel) or rel
            if os.path.isabs(rel) or rel.split("/", 1)[0] not in {
                "src",
                "harness",
                "tests",
                "bin",
                ".claude",
                ".codex",
                ".agents",
            }:
                return ""
        why = activity_gate.edit_refusal(root, session_id=session_id, branch=branch)
        return deny(why) if why else ""
    except Exception as exc:  # noqa: BLE001 — edit hooks fail open
        sys.stderr.write(f"note: activity check failed ({exc}); allowing the edit\n")
        return ""


def check_command(root: Path, cmd: str, *, payload: dict[str, Any] | None = None) -> str:
    """The shell-write denial for one Bash command, or "".

    On Windows a `\\` means two things depending on the shell, and the hook cannot tell which
    shell runs the command. Git Bash (Claude's shell) reads it as an escape: `STATUS\\.md` is
    `STATUS.md`. PowerShell and cmd read it as a separator: `.agent\\status.json`.
    POSIX parsing alone turned the second into `.agentstatus.json` and missed it; swapping every
    `\\` for `/` alone turned the first into `STATUS/.md` and missed that. So check both readings and deny if either writes a protected file."""
    for reading in shell_readings(cmd):
        denial = _check_tokens(root, _op_tokens(reading))
        if denial:
            return denial
        for target in _write_targets(_op_tokens(reading), reading):
            if denial := activity_guard(root, payload or {}, target):
                return denial
    return ""


ENV_VALUE_OPTIONS = frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"})


def _quoted_operator_flags(cmd: str, toks: list[str]) -> list[bool]:
    """Per token of `_op_tokens(cmd)`: True where an operator-shaped token was quoted data
    (`rg '|' x`). Re-lexes without quote removal; if the two lexings disagree, nothing is quoted."""
    text = drop_heredoc_bodies(cmd).replace("\\\n", " ")
    lexer = shlex.shlex(text, posix=False, punctuation_chars=">|<&;")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        raw = [t for t in lexer if t.strip()]
    except ValueError:
        return [False] * len(toks)
    if len(raw) != len(toks):
        return [False] * len(toks)
    return [
        bool(t) and set(t) <= set("|&;") and not set(r) <= set("|&;") for t, r in zip(toks, raw, strict=True)
    ]


def _writer_operands(segment: list[str]) -> list[str]:
    """Operands of a raw writer that is the command of one pipeline segment, after any `env`
    wrapper. The same names as arguments (`rg touch src/`) are search words, not writes."""
    i = 0
    while i < len(segment) and ENV_ASSIGNMENT_RE.match(segment[i]):
        i += 1
    if i < len(segment) and os.path.basename(segment[i]) == "env":
        i += 1
        while i < len(segment) and (segment[i].startswith("-") or ENV_ASSIGNMENT_RE.match(segment[i])):
            i += 2 if segment[i] in ENV_VALUE_OPTIONS else 1
    if i >= len(segment):
        return []
    head, rest = os.path.basename(segment[i]), segment[i + 1 :]
    if head in ("rm", "truncate", "dd", "install", "touch") or (
        head in ("sed", "perl") and any(IN_PLACE_FLAG_RE.match(t) for t in rest)
    ):
        return [t for t in rest if not t.startswith("-")]
    return []


def _write_targets(toks: list[str], cmd: str = "") -> list[str]:
    """The shell writes recognized by check_command; interpreters and formatters are outside it."""
    quoted = _quoted_operator_flags(cmd, toks) if cmd else [False] * len(toks)
    while toks and ENV_ASSIGNMENT_RE.match(toks[0]):
        toks, quoted = toks[1:], quoted[1:]
    if not toks:
        return []
    bases = [os.path.basename(t) for t in toks]
    targets: list[str] = []
    if bases[0] in ("cp", "mv", "ln") and len(toks) > 1:
        targets.append(toks[-1])
    segment: list[str] = []
    for tok, is_data in zip([*toks, ";"], [*quoted, False], strict=True):
        if tok and set(tok) <= set("|&;") and not is_data:
            targets.extend(_writer_operands(segment))
            segment = []
        else:
            segment.append(tok)
    for i, tok in enumerate(toks):
        if tok in (">", ">>", ">|") and i + 1 < len(toks):
            targets.append(toks[i + 1])
        if os.path.basename(tok) == "tee":
            for target in toks[i + 1 :]:
                if target in ("|", "&", ";", ">", ">>"):
                    break
                if not target.startswith("-"):
                    targets.append(target)
    return [t.split("=", 1)[1] if t.startswith(("of=", "file=")) else t for t in targets]


def shell_readings(cmd: str) -> list[str]:
    """Every way a shell might read `cmd`, for guards that tokenize it. On Windows a `\\` is an
    escape to Git Bash (Claude's shell: `STATUS\\.md`, `Agent\\ notes`) and a separator to
    PowerShell and cmd (Codex's: `.agent\\status.json`), and a hook cannot tell which shell will
    run it. A guard checks each reading and denies if any would. Rewriting every backslash alone
    broke Git Bash escapes, twice; use this, not a local copy."""
    readings = [cmd]
    if sys.platform == "win32" and "\\" in cmd:
        readings.append(slashes(cmd))
    return readings


def _check_tokens(root: Path, toks: list[str]) -> str:
    # Skip leading environment assignments before classifying the command, so overrides do not hide
    # protected-path writes.
    while toks and ENV_ASSIGNMENT_RE.match(toks[0]):
        toks = toks[1:]
    if not toks:
        return ""
    bases = [os.path.basename(t) for t in toks]
    # rm -r or -f on the record or the harness
    if "rm" in bases:
        i = bases.index("rm")
        rest = toks[i + 1 :]
        if any(DELETE_FLAG_RE.match(t) for t in rest) and any(_protected(t, dirs=True) for t in rest):
            return deny(
                "recursive or forced delete inside the harness is not allowed. Close features with "
                "the feature_close tool; supersede ADRs with the adr_new tool and `supersedes`."
            )
    if "find" in bases and any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for t in toks):
        return deny(
            "find -delete/-exec is not allowed. List the matches with `find … -print`, then run a "
            "read-only command on those paths (`wc -l a b`, `shasum a b`), or act one path at a time."
        )
    # In-place editors and raw writers.
    if (
        ("sed" in bases or "perl" in bases)
        and any(IN_PLACE_FLAG_RE.match(t) for t in toks)
        and any(_writes_protected(root, t) for t in toks)
    ):
        return deny(SHELL_WRITE_REASON)
    if any(b in ("truncate", "dd", "install", "touch") for b in bases) and any(
        _writes_protected(root, t) for t in toks[1:]
    ):
        return deny(SHELL_WRITE_REASON)
    # cp, mv, ln: the destination is the last argument.
    if bases[0] in ("cp", "mv", "ln") and len(toks) >= 2 and _writes_protected(root, toks[-1]):
        return deny(SHELL_WRITE_REASON)
    # Redirects and tee.
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
    # Treat build, test, install and dev/start/setup scripts as artifact generation rather than
    # code edits, including --dir, -C and --filter forms. sed -n, perl -ne and awk reads remain
    # allowed without in-place flags. Protected-path in-place writes are denied first; product-code
    # writes fall through here.
    reads_only = (bases[0] in READ_ONLY_HEADS and not _writing_option(bases[0], toks)) or (
        bases[0] in STREAM_EDITORS and not any(IN_PLACE_FLAG_RE.match(t) for t in toks)
    )
    if not reads_only and toks[0] != "bin/agent" and not _operates_not_writes(toks, bases):
        for t in toks[1:]:
            if _outside_spelling(t) and under_registered_repo(root, t):
                return deny(PRODUCT_CODE_REASON)
    return ""


def _outside_spelling(tok: str) -> bool:
    """A token spelled to reach outside the working directory: absolute, or up from it. On
    Windows that includes `C:\\x`, `C:/x` and `\\\\server\\share`, which name the same repos
    a POSIX `/x` does."""
    if tok.startswith(("/", "../")):
        return True
    return sys.platform == "win32" and (ntpath.isabs(tok) or tok.startswith("..\\"))


def _writing_option(head: str, toks: list[str]) -> bool:
    """A read-only command's option that writes a file: `file -C` compiles a magic file and
    `tree -o` writes its listing."""
    return (
        head == "file" and any(t.startswith("-") and "C" in t for t in toks[1:] if not t.startswith("--"))
    ) or (head == "tree" and any(t == "-o" or t.startswith("-o") for t in toks[1:]))


def shell_tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()
