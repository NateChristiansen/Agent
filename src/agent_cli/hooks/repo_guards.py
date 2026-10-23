"""`pull-warn` and `push-guard`: the two PreToolUse hooks about a registered clone's freshness
and review state. `pull-warn` is advisory (a read against a stale
clone); `push-guard` is a deny (an unreviewed branch pushed to a remote), the early warning of
git's pre-push gate (`push_review`).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import push_review, sessions
from ..push_review import OVERRIDE_ENV as PUSH_OVERRIDE_ENV
from ..records.narrow import as_dict, dig
from .paths import deny, shell_tokens

if TYPE_CHECKING:
    from ..repos import Repo

# ------------------------------------------------------------------ pull-warn

PULLED_KEY = "pulled"
PULL_WARNED_KEY = "pull_warned"
PULL_WARN_TOOLS = ("Read", "Grep", "Glob")


def _repo_for_path(root: Path, file_path: str) -> str | None:
    """The registered clone's short whose directory contains this path, or None.

    Path-only where `git_discovery` can answer; otherwise one short `git rev-parse` per side,
    so the hook stays inside its five-second PreToolUse budget."""
    if not file_path:
        return None
    from .. import repos as repos_mod

    try:
        target = Path(file_path).expanduser()
        target = target if target.is_absolute() else (root / target)
        target = target.resolve()
    except OSError:
        return None
    for repo in repos_mod.load_registry(root).repos:
        base = repo.abs_path(root)
        if target == base or base in target.parents:
            if base == root.resolve():
                continue
            try:
                common = push_review.common_dir(root)
                if common is not None and common == push_review.common_dir(base):
                    continue
            except OSError, repos_mod.ExternalFailure:
                pass
            return repo.short
    return None


def pull_warn(root: Path, payload: dict[str, Any]) -> str:
    """PreToolUse on Read|Grep|Glob: the first read under a registered clone this session has
    not pulled.

    A clone that is behind its remote reports stale facts as current. `repos sync --pull` is
    the rule, and this checks it at the moment of the read. Advisory only, never a denial, since the
    read may be deliberate (a worktree, a historical check). Fires once per repo per session,
    recorded under `pull_warned`, so a sweep of thirty files costs one line.

    Fails open on anything: an unreadable registry, a path that will not resolve, a session
    with no id. A warning that wedges a Read is worse than a stale fact."""
    if payload.get("agent_id") or payload.get("agent_type"):
        return ""
    if str(payload.get("tool_name") or "") not in PULL_WARN_TOOLS:
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    tool_input = as_dict(payload.get("tool_input"))
    short = _repo_for_path(root, str(tool_input.get("file_path") or tool_input.get("path") or ""))
    if not short:
        return ""
    if short in sessions.get_session_list(root, session_id, PULLED_KEY):
        return ""
    if short in sessions.get_session_list(root, session_id, PULL_WARNED_KEY):
        return ""
    sessions.add_to_session_list(root, session_id, PULL_WARNED_KEY, short)
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"{short} has not been pulled in this session; what you read may be stale. "
                    f'Run the repos_sync tool with pull=True (name="{short}") before reporting anything from it.'
                ),
            }
        }
    )


# ------------------------------------------------------------------ push-guard

REVIEW_ROW = "review"
PUSH_GUARD_REASON = (
    "this push names no branch: a detached head in a dispatch's worktree has no branch to check a "
    "code-review round for (ADR-0063). Push it as a named branch, for example `git push origin "
    "HEAD:<branch>`."
)
AUTHORITY = (
    "This is push-guard's early warning; git's pre-push hook checks the exact refs the push sends "
    "and decides."
)


def push_guard(root: Path, payload: dict[str, Any]) -> str:
    """Warn early, before git runs, when a push the command text names sends a guarded ref with
    no clean `code-review` round (ADR-0063). Git's `pre-push` hook is the authority
    (`push_review`): it reads the exact refs, so a push whose refs this parse cannot list (a bulk
    flag, a glob, no refspec, an option it does not know, a word whose value depends on the shell)
    passes here and is decided there. The scope, the round check and the reasons are
    `push_review`'s.

    `AGENT_PUSH_UNREVIEWED=1` lets every ref through, read as git will see it (`_override`), and
    push-guard writes no note: pre-push writes every override note, and the hook-bypass guard
    denies a push that would skip it in the harness or a registered product clone. Fail open on
    unreadable state so a slow Git operation cannot wedge a legitimate push: an override value
    the shell decides, a source commit git cannot resolve, and a round file of the head that is
    not readable JSON, let the push or that ref through without a denial. Fail closed only on a
    round record that reads but lacks its branch or head, which is not clean.
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return ""
    command = str(dig(payload, "tool_input", "command") or "")
    if not _is_git_push(command):
        return ""
    override = _override(command)
    if override is None:
        return ""
    where = _push_cwd(command, str(payload.get("cwd") or root))
    refs = _refspecs(command, where)
    if refs is None:
        return ""
    from .. import dispatch as dispatch_mod

    all_records = dispatch_mod.load_all(root)
    held = push_review.worktree_records(all_records, where)
    repo, others = _pushed_repo(root, held, where)
    name = repo.name if repo else None
    denials: list[str] = []
    for source, branch in refs:
        mine = push_review.guarding(all_records, held, repo, {branch, _ref_name(source, where)}, others)
        if not mine:
            continue
        if not branch:
            return deny(f"{PUSH_GUARD_REASON} {AUTHORITY}")
        if override:
            continue
        head = push_review.git_line(where, ["rev-parse", "--verify", "--quiet", f"{source}^{{commit}}"])
        if head is None:
            continue
        why = push_review.unclean(root, head, name, mine, branch, strict=False)
        if why is not None:
            how = f"prefix the command with {PUSH_OVERRIDE_ENV}=1"
            denials.append(push_review.reason(mine, branch, head, why, how=how, checkout=where, repo=name))
    return deny(" ".join([*denials, AUTHORITY])) if denials else ""


_SHELL_EXPANDS = ("$", "`")


def _override(command: str) -> bool | None:
    """Whether git's environment will hold `AGENT_PUSH_UNREVIEWED=1` exactly: the last of the
    command's leading assignments to it, else push-guard's own environment, which the shell
    passes on. None when the shell decides the value: it expands, or the assignment is its own
    statement (`AGENT_PUSH_UNREVIEWED=1;`), which reaches git only if the name is exported."""
    value: str | None = None
    for word in _env_prefix(command):
        name, _, assigned = word.partition("=")
        if name == PUSH_OVERRIDE_ENV:
            value = assigned
    if value is None:
        return os.environ.get(PUSH_OVERRIDE_ENV) == "1"
    if _CONTROL_RE.search(value) or any(c in value for c in _SHELL_EXPANDS):
        return None
    return value == "1"


#: Push options that change neither which refs are sent nor where they go.
_PUSH_FLAGS = frozenset(
    {
        "-f", "--force", "-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--no-verify",
        "--verify", "--force-with-lease", "--force-if-includes", "--no-force-if-includes", "--atomic",
        "--no-atomic", "-n", "--dry-run", "--porcelain", "--progress", "--no-progress",
    }
)  # fmt: skip
_PUSH_ARG_FLAGS = ("-o", "--push-option")
_BULK_FLAGS = frozenset({"--all", "--branches", "--mirror", "--tags", "--follow-tags", "--prune"})
_DELETE_FLAGS = frozenset({"-d", "--delete"})
_CONTROL_RE = re.compile(r"[;&|<>]")


def _command_words(command: str) -> list[str]:
    """The push's own words: `_push_tokens` cut at the first shell control operator or
    redirection (`&&`, `|`, `;`, `2>&1`), so what follows is not read as a refspec."""
    out: list[str] = []
    for tok in _push_tokens(command):
        m = _CONTROL_RE.search(tok)
        if m is None:
            out.append(tok)
            continue
        head = tok[: m.start()]
        if head and not head.isdigit():
            out.append(head)
        break
    return out


def _refspecs(command: str, where: Path) -> list[tuple[str, str]] | None:
    """Each ref the push sends, as (source, destination branch), or None when the refs cannot be
    listed exactly: a word the shell expands (`"$B"`, a backtick, `$(...)`), a bulk flag, a glob or
    matching (`:`) refspec, no refspec at all (git then pushes what its configuration says), or an
    option this parse does not know. Those are the pre-push hook's to decide.
    `+HEAD:refs/heads/agent/016` is (`HEAD`, `agent/016`) and a bare `agent/016` is itself on both
    sides. With `--repo`, every positional word is read as a refspec. A deletion (`:agent/016`, or
    any refspec under `--delete`) sends no commit and is left out. A destination of `HEAD` on a
    detached checkout reads as no branch."""
    words = _command_words(command)
    if any(c in word for word in words for c in _SHELL_EXPANDS):
        return None
    i = 1
    while i < len(words) and words[i] != "push":
        if words[i] in ("-C", "-c"):
            i += 2
        elif words[i] == "--no-pager":
            i += 1
        else:
            return None
    remote: str | None = None
    specs: list[str] = []
    delete = positional = False
    rest = words[i + 1 :]
    j = 0
    while j < len(rest):
        tok = rest[j]
        j += 1
        if positional or not tok.startswith("-") or tok == "-":
            if remote is None:
                remote = tok
            else:
                specs.append(tok)
        elif tok == "--":
            positional = True
        elif tok in _BULK_FLAGS:
            return None
        elif tok in _DELETE_FLAGS:
            delete = True
        elif tok in _PUSH_ARG_FLAGS:
            j += 1
        elif tok == "--repo" and j < len(rest):
            remote, j = rest[j], j + 1
        elif tok.startswith("--repo="):
            remote = tok.removeprefix("--repo=")
        elif not (tok in _PUSH_FLAGS or tok.startswith(("--force-with-lease=", "--push-option="))):
            return None
    if not specs:
        return None
    out: list[tuple[str, str]] = []
    for spec in specs:
        plain = spec.removeprefix("+")
        if "*" in plain or plain == ":":
            return None
        source, colon, dest = plain.partition(":")
        if delete or not source:
            continue
        dest = (dest if colon else source).removeprefix("refs/heads/")
        out.append((source, _current_branch(where) if dest == "HEAD" else dest))
    return out


def _ref_name(source: str, where: Path) -> str:
    """The branch a refspec's source names: the checked-out one for `HEAD`."""
    return _current_branch(where) if source == "HEAD" else source.removeprefix("refs/heads/")


def _pushed_repo(root: Path, held: list[dict[str, Any]], where: Path) -> tuple[Repo | None, frozenset[str]]:
    """`push_review.repo_of` over the registry, with `push_review.harness_others`; an unreadable
    registry leaves the repo unknown."""
    from .. import repos as repos_mod

    try:
        registry = repos_mod.load_registry(root).repos
    except Exception:  # noqa: BLE001 — an unreadable registry leaves the repo unknown
        return None, frozenset()
    repo = push_review.repo_of(root, registry, held, where)
    return repo, push_review.harness_others(root, registry, repo, where)


_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _env_prefix(command: str) -> list[str]:
    """The leading `VAR=value` words of `command`, as the shell reads them."""
    toks = shell_tokens(command)
    i = 0
    while i < len(toks) and _ENV_PREFIX_RE.match(toks[i]):
        i += 1
    return toks[:i]


def _push_tokens(command: str) -> list[str]:
    """`command` with any leading `VAR=value` env prefix dropped.

    So `AGENT_PUSH_UNREVIEWED=1 git push` still parses as a git push."""
    return shell_tokens(command)[len(_env_prefix(command)) :]


_GIT_FLAGS_WITH_ARG = ("-C", "--git-dir", "--work-tree", "--namespace", "-c")


def _is_git_push(command: str) -> bool:
    """`git push`, `git -C <path> push`, `VAR=1 git push`. Matches the verb, not a substring.

    A command that merely mentions the word push (a grep, a commit message) is not one."""
    toks = _push_tokens(command)
    if not toks or os.path.basename(toks[0]) != "git":
        return False
    i = 1
    while i < len(toks):
        tok = toks[i]
        if tok in _GIT_FLAGS_WITH_ARG:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok == "push"
    return False


def _push_cwd(command: str, cwd: str) -> Path:
    """Where git runs the push: `cwd`, then each `-C <path>` before the verb in turn, read from
    the shell's words so a quoted path is the path."""
    where = Path(cwd)
    words = _command_words(command)
    i = 1
    while i < len(words) and words[i] != "push":
        if words[i] == "-C" and i + 1 < len(words):
            where = where / words[i + 1]
        i += 2 if words[i] in _GIT_FLAGS_WITH_ARG else 1
    return where


def _current_branch(cwd: Path) -> str:
    name = push_review.git_line(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""
    return "" if name == "HEAD" else name
