"""Require file leases before committing shared append-only records during manager overlap."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import leases, notes, sessions
from ..core import AgentError, is_alive
from ..records.narrow import dig
from ..session_liveness import record_live
from .main_guard import main_guard
from .paths import deny, shell_readings, shell_tokens

_FEATURE_LOG = re.compile(r"^features/[0-9]{3,}-[a-z0-9-]+/log\.md$")
_CALIBRATION = {"harness/dispatch-rubric.md", "harness/owner/calibration.md"}
_GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")
_MSYS_TMP = re.compile(r"^/tmp(/|$)")


def native_path(value: str) -> str:
    """A `git -C` path as this OS reads it. Git Bash writes `/c/Users/...`, which Windows Python
    joins onto the current drive as `C:/c/Users/...`: the guard's own git call then failed and
    it denied a notes-repo commit with no reason.

    Git Bash mounts `/tmp` on the user's temp directory, not on `C:\\tmp`: `git -C /tmp/x commit`
    was denied with WinError 267 while another manager was live."""
    if sys.platform != "win32":
        return value
    if m := _MSYS_DRIVE.match(value):
        return f"{m.group(1).upper()}:/{value[m.end() :]}"
    if m := _MSYS_TMP.match(value):
        import tempfile

        return f"{Path(tempfile.gettempdir()).as_posix()}/{value[m.end() :]}"
    return value


def git_calls(command: str, cwd: Path) -> list[tuple[Path, str, list[str], list[str]]]:
    """`_git_call` for every shell reading of `command` that parses as a git command; either
    reading can deny (`paths.shell_readings`)."""
    return [call for text in shell_readings(command) if (call := _git_call(text, cwd)) is not None]


def _git_call(command: str, cwd: Path) -> tuple[Path, str, list[str], list[str]] | None:
    """`(cwd, subcommand, its args, git's own options)` for a plain `git` command, else None.
    Parses one reading of the command; `git_calls` tries each."""
    tokens = shell_tokens(command)
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    if not tokens or os.path.basename(tokens[0]) != "git":
        return None
    at = cwd
    options: list[str] = []
    i = 1
    while i < len(tokens):
        arg = tokens[i]
        if arg in _GIT_OPTIONS_WITH_VALUE:
            if i + 1 >= len(tokens):
                return None
            if arg == "-C":
                at = at / native_path(tokens[i + 1])
            options += tokens[i : i + 2]
            i += 2
            continue
        if arg.startswith("-"):
            options.append(arg)
            i += 1
            continue
        return at, arg, tokens[i + 1 :], options
    return None


def _without_dir(options: list[str]) -> list[str]:
    """Git's own options minus `-C <dir>`, which `_git_call` has already applied to the cwd."""
    out: list[str] = []
    skip = False
    for i, option in enumerate(options):
        if skip:
            skip = False
        elif option == "-C" and i + 1 < len(options):
            skip = True
        else:
            out.append(option)
    return out


def commit_calls(command: str, cwd: Path) -> list[tuple[str, Path]]:
    """`(reading, repo dir)` for each reading of `command` that is a `git commit`. Every reading
    is kept, even two in the same directory: they can differ in pathspec (`-- src\\x.py`), and
    only one of them may name code. There are at most two."""
    out: list[tuple[str, Path]] = []
    for text in shell_readings(command):
        call = _git_call(text, cwd)
        if call and call[1] == "commit":
            out.append((text, call[0]))
    return out


# `git commit` short options whose value follows in the same token (`-mfix`): a letter after
# one of these is part of the value, not a flag.
_COMMIT_VALUE_FLAGS = set("mFcCtS")
_COMMIT_LONG_VALUE_OPTIONS = {
    "--message",
    "--file",
    "--author",
    "--date",
    "--template",
    "--reuse-message",
    "--reedit-message",
    "--fixup",
    "--squash",
    "--trailer",
    "--cleanup",
}


def _skips_hooks(sub: str, args: list[str], options: list[str]) -> bool:
    """True when the command would run without this repo's git hooks (see ADR-0026)."""
    if sub not in ("commit", "push", "merge"):
        return False  # a read such as `rev-parse` runs no hook, so an override there is harmless
    if any("hookspath" in o.lower() for o in options):
        return True
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False  # the value of `-m`, `-F` and the like, however it reads
            continue
        # A redirect, heredoc or pipe ends git's arguments; a heredoc body is message text.
        if arg == "--" or arg[:1] in ("<", ">", "|"):
            break
        if arg == "--no-verify":
            return True
        if arg in _COMMIT_LONG_VALUE_OPTIONS:
            skip_next = True
            continue
        # `push -n` is `--dry-run`; only commit's `-n` is `--no-verify`.
        if sub == "commit" and arg.startswith("-") and not arg.startswith("--"):
            for at, letter in enumerate(arg[1:], start=1):
                if letter == "n":
                    return True
                if letter in _COMMIT_VALUE_FLAGS:
                    skip_next = at == len(arg) - 1  # `-am msg`: the value is the next token
                    break
    return False


PUSH_BYPASS_REASON = (
    "git's pre-push hook is this repo's publish and code-review gate (ADR-0026, ADR-0063); do not "
    "skip it with --no-verify or core.hooksPath. To push a ref past the code-review gate, set "
    "AGENT_PUSH_UNREVIEWED=1 in the push's environment and let the hook run: it logs the override "
    "to the feature."
)


def _git_dir(command: str, at: Path, options: list[str]) -> Path | None:
    """The repository a git call names instead of the one around `at`: its last `--git-dir`
    (either spelling), else a leading `GIT_DIR=` assignment, relative to `at` as git reads it.
    Its hooks are the ones git runs, whatever the working directory."""
    named: str | None = None
    for word in shell_tokens(command):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            break
        if word.startswith("GIT_DIR="):
            named = word.removeprefix("GIT_DIR=")
    for i, option in enumerate(options):
        if option == "--git-dir" and i + 1 < len(options):
            named = options[i + 1]
        elif option.startswith("--git-dir="):
            named = option.removeprefix("--git-dir=")
    if not named:
        return None
    return at / os.path.expanduser(native_path(named))


def _push_gated(root: Path, at: Path) -> bool:
    """Whether a push from `at` is the review gate's: `at` shares the git common dir of the
    harness or of a registered product clone, so a linked worktree of either counts. A checkout
    git cannot place is gated, as a commit's is; an unreadable registry leaves the harness only."""
    from .. import push_review
    from .. import repos as repos_mod

    common = push_review.safe_common_dir(at)
    if common is None:
        return True
    try:
        checkouts = [repo.abs_path(root) for repo in repos_mod.load_registry(root).repos]
    except Exception:  # noqa: BLE001 — an unreadable registry keeps the harness-only scope
        checkouts = []
    return any(push_review.safe_common_dir(path) == common for path in [root, *checkouts])


def hook_bypass_guard(root: Path, payload: dict[str, Any]) -> str:
    """Deny a `git commit` or `git merge` in this checkout that skips its hooks, the pre-commit
    gate on format and size, and a `git push` that skips them in the harness, a registered product
    clone, or a worktree of either: pre-push is the code-review gate and writes every override
    note, so it always runs there."""
    if payload.get("tool_name") != "Bash":
        return ""
    command = str(dig(payload, "tool_input", "command") or "")
    cwd = Path(str(payload.get("cwd") or root))
    for text in shell_readings(command):
        call = _git_call(text, cwd)
        if call is None or not _skips_hooks(call[1], call[2], call[3]):
            continue
        if call[1] == "push":
            if _push_gated(root, _git_dir(text, call[0], call[3]) or call[0]):
                return deny(PUSH_BYPASS_REASON)
            continue
        top = _git(call[0], "rev-parse", "--show-toplevel")
        if top is not None and Path(top.strip()).resolve() != root.resolve():
            continue  # another repo's hooks are its own business
        return deny(
            "this repo's git hooks are its format, size and publish gates (ADR-0026); do not skip "
            "them with --no-verify, -n or core.hooksPath. Fix what the hook reports, or ask the owner."
        )
    return ""


def _git_run(cwd: Path, *args: str) -> tuple[str | None, str]:
    """`(stdout, "")` when git succeeds at `cwd`, else `(None, why)` from this same call: a
    denial names the failure it saw, not a second call's, which may pass after a first timeout."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2, check=False)
    except subprocess.TimeoutExpired:
        return None, f"git {args[0]} timed out after 2 s in {cwd}"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git could not run in {cwd}: {exc}"
    if proc.returncode == 0:
        return proc.stdout, ""
    lines = (proc.stderr or proc.stdout).strip().splitlines()
    return None, lines[-1] if lines else f"git {args[0]} exited {proc.returncode} in {cwd}"


def _git(cwd: Path, *args: str) -> str | None:
    return _git_run(cwd, *args)[0]


def other_managers_live(root: Path, session_id: str) -> bool:
    try:
        return any(
            record.get("session_id") != session_id
            and not record.get("dispatch_id")
            and record_live(record, is_alive)
            and dig(record, "activity", "state") != "ended"
            for record in sessions.all_records(root)
        )
    except AgentError, OSError, ValueError:
        return True  # unreadable session state cannot prove the commit is uncontended


def commit_guard(root: Path, payload: dict[str, Any]) -> str:
    """Deny a staged append-only path without this session's live, exact file lease, and any
    commit or push here that skips the git hooks."""
    if payload.get("tool_name") != "Bash":
        return ""
    denial = hook_bypass_guard(root, payload)
    if denial:
        return denial
    command = str(dig(payload, "tool_input", "command") or "")
    session_id = str(payload.get("session_id") or "")
    for at, verb, args, options in git_calls(command, Path(str(payload.get("cwd") or root))):
        if denial := main_guard(root, at, verb, args, _without_dir(options)):
            return denial
    for _text, cwd in commit_calls(command, Path(str(payload.get("cwd") or root))):
        denial = _lease_gate(root, cwd, session_id)
        if denial:
            return denial
    return ""


def _take_uncontended(root: Path, area: str, session_id: str) -> bool:
    """Take `area` for this session when no live holder overlaps it. All four commit-guard lease
    denials in the audit week cleared in seconds by exactly this take; a live holder still
    refuses, and nothing is ever taken from one."""
    try:
        if leases.blocker(root, area, session_id) is not None:
            return False
        leases.take(root, area, session_id=session_id, pid=sessions.resolve_pid())
        return True
    except AgentError, OSError, ValueError:
        return False


def _lease_gate(root: Path, cwd: Path, session_id: str) -> str:
    """The append-only lease check for one commit reading, run only while another manager is live."""
    if not other_managers_live(root, session_id):
        return ""
    top, why = _git_run(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return deny(f"cannot inspect the Git checkout for the append-only commit lease gate: {why}")
    repo_root = Path(top.strip()).resolve()
    notes_root = notes.read_notes_root(root)
    if repo_root not in {root.resolve(), notes_root.resolve() if notes_root else root.resolve()}:
        return ""
    staged = _git(cwd, "diff", "--cached", "--name-only", "-z")
    if staged is None:
        return deny("cannot inspect staged files for the append-only commit lease gate")
    protected = sorted(
        path for path in staged.split("\0") if _FEATURE_LOG.fullmatch(path) or path in _CALIBRATION
    )
    missing: list[str] = []
    for path in protected:
        area = f"vault:{path}"
        try:
            held = leases.get(root, area)
            owned = (
                held is not None
                and held.get("session_id") == session_id
                and leases.status(held, root) in leases.BLOCKING_STATUSES
            )
        except AgentError, OSError, ValueError:
            owned = False
        if not owned and not _take_uncontended(root, area, session_id):
            missing.append(path)
    if missing:
        return deny(
            "shared append-only staged files need this session's exclusive file lease while "
            "another manager is live: "
            + ", ".join(missing)
            + ". Take each with the `lease` tool, action `take`, area `vault:<path>`."
        )
    return ""
