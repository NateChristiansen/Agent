"""`pull-warn` and `push-guard`: the two PreToolUse hooks about a registered clone's freshness
and review state. `pull-warn` is advisory (a read against a stale
clone); `push-guard` is a deny (an unreviewed branch pushed to a remote).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import sessions
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
                common = _common_dir(root)
                if common is not None and common == _common_dir(base):
                    continue
            except OSError, repos_mod.ExternalFailure:
                pass
            return repo.short
    return None


def _common_dir(start: Path) -> Path | None:
    """`git_discovery.common_dir`, else git's answer (on Windows the ownership check always
    defers to git). None when neither can say."""
    from .. import git_discovery

    common = git_discovery.common_dir(start)
    if common is not None:
        return common
    out = _git_line(start, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if not out:
        return None
    try:
        return Path(out).resolve()
    except OSError:
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

PUSH_OVERRIDE_ENV = "AGENT_PUSH_UNREVIEWED"
REVIEW_ROW = "review"
REVIEW_REASON_RE = re.compile(r"^\s*review(?=[:,]|\s+row\b)|(?:^|;\s*)rubric row review:", re.I)
PUSH_GUARD_REASON = (
    "this push names no branch: a detached head in a dispatch's worktree has no branch to check a "
    "code-review round for (ADR-0063). Push it as a named branch, for example `git push origin "
    "HEAD:<branch>`."
)


def _unlisted_reason(why: str) -> str:
    return (
        f"push-guard cannot list exactly which refs this push sends ({why}), so it refuses it "
        "(ADR-0063). Name each branch on the command line, for example `git push origin agent/<name>` "
        "or `git push origin HEAD:agent/<name>`. To push anyway, prefix the command with "
        f"{PUSH_OVERRIDE_ENV}=1 (it is logged to the feature)."
    )


def push_guard(root: Path, payload: dict[str, Any]) -> str:
    """Require a clean `code-review` round (ADR-0063) on the commit each guarded ref of the push
    sends. A ref is guarded when a dispatch record in the pushed repo names its source or its
    destination branch, and every ref pushed from a dispatch's worktree is, so a renamed branch is
    guarded too. Other manager branches are outside this guard.

    In the harness and registered repos, a push whose refs `_refspecs` cannot list exactly is
    refused. AGENT_PUSH_UNREVIEWED=1 overrides either refusal and logs a feature note. Fail open
    on unreadable state so a slow Git operation cannot wedge a legitimate push: a source commit
    git cannot resolve, and a round file of the head that is not readable JSON, let that ref
    through. A round record that reads but lacks its branch or head is not clean.
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return ""
    command = str(dig(payload, "tool_input", "command") or "")
    if not _is_git_push(command):
        return ""
    where = _push_cwd(command, str(payload.get("cwd") or root))
    from .. import dispatch as dispatch_mod

    all_records = dispatch_mod.load_all(root)
    held = _worktree_records(all_records, where)
    override = PUSH_OVERRIDE_ENV in command
    repo, known = _pushed_repo(root, held, where)
    refs, unlisted = _refspecs(command, where)
    if refs is None:
        if not known:
            return ""
        if override:
            _note_unlisted_push(root, held, unlisted)
            return ""
        return deny(_unlisted_reason(unlisted))
    denials: list[str] = []
    for source, branch in refs:
        names = {branch, _ref_name(source, where)} - {""}
        named = [d for d in all_records if str(d.get("branch") or "") in names]
        if repo is not None:
            # A record with no repo is not proof of another repo, so it stays.
            named = [d for d in named if str(d.get("repo") or repo.name) in (repo.name, repo.short)]
        mine = named + [d for d in held if d not in named]
        if not mine:
            continue
        if not branch:
            return deny(PUSH_GUARD_REASON)
        head = _git_line(where, ["rev-parse", "--verify", "--quiet", f"{source}^{{commit}}"])
        why = _unclean(root, head, repo.name if repo else None, mine, branch) if head else None
        if head is None or why is None:
            continue
        if override:
            _note_unreviewed_push(root, mine, branch, head, why)
        else:
            denials.append(_code_review_reason(mine, branch, head, why))
    return deny(" ".join(denials)) if denials else ""


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


def _refspecs(command: str, where: Path) -> tuple[list[tuple[str, str]] | None, str]:
    """Each ref the push sends, as (source, destination branch), or None and why when the
    refs cannot be listed exactly: a bulk flag, a glob or matching (`:`) refspec, no refspec at
    all (git then pushes what its configuration says), or an option this parse does not know.
    `+HEAD:refs/heads/agent/016` is (`HEAD`, `agent/016`) and a bare `agent/016` is itself on both
    sides. With `--repo`, every positional word is read as a refspec. A deletion (`:agent/016`,
    or any refspec under `--delete`) sends no commit and is left out. A destination of `HEAD` on
    a detached checkout reads as no branch."""
    words = _command_words(command)
    i = 1
    while i < len(words) and words[i] != "push":
        if words[i] in ("-C", "-c"):
            i += 2
        elif words[i] == "--no-pager":
            i += 1
        else:
            return None, f"the git option `{words[i]}`, which push-guard does not parse"
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
            return None, f"`{tok}` pushes refs the command does not name"
        elif tok in _DELETE_FLAGS:
            delete = True
        elif tok in _PUSH_ARG_FLAGS:
            j += 1
        elif tok == "--repo" and j < len(rest):
            remote, j = rest[j], j + 1
        elif tok.startswith("--repo="):
            remote = tok.removeprefix("--repo=")
        elif not (tok in _PUSH_FLAGS or tok.startswith(("--force-with-lease=", "--push-option="))):
            return None, f"the option `{tok}`, which push-guard does not parse"
    if not specs:
        return None, "no refspec, so git pushes what its configuration says"
    out: list[tuple[str, str]] = []
    for spec in specs:
        plain = spec.removeprefix("+")
        if "*" in plain:
            return None, f"the glob refspec `{spec}`"
        if plain == ":":
            return None, "the matching refspec `:`"
        source, colon, dest = plain.partition(":")
        if delete or not source:
            continue
        dest = (dest if colon else source).removeprefix("refs/heads/")
        out.append((source, _current_branch(where) if dest == "HEAD" else dest))
    return out, ""


def _ref_name(source: str, where: Path) -> str:
    """The branch a refspec's source names: the checked-out one for `HEAD`."""
    return _current_branch(where) if source == "HEAD" else source.removeprefix("refs/heads/")


def _pushed_repo(root: Path, held: list[dict[str, Any]], where: Path) -> tuple[Repo | None, bool]:
    """The registered repo the push comes from, and whether it is the harness or a registered
    repo. A dispatch worktree's repo first; else the registered repo sharing `where`'s git common
    directory, so a linked worktree outside the checkout resolves too; else the registered
    checkout holding `where` when git cannot say (the deepest, for a nested clone). The harness
    checkout counts as known even when the registry does not list it."""
    from .. import repos as repos_mod

    try:
        registry = repos_mod.load_registry(root).repos
        target = where.expanduser().resolve()
    except Exception:  # noqa: BLE001 — an unreadable registry leaves the repo unknown
        return None, False
    named = {str(d.get("repo") or "") for d in held} - {""}
    if held:
        return next((r for r in registry if named & {r.name, r.short}), None), True
    common = _safe_common_dir(target)
    if common is not None:
        for r in registry:
            if _safe_common_dir(r.abs_path(root)) == common:
                return r, True
        return None, common == _safe_common_dir(root)
    holding: list[tuple[int, Repo]] = []
    for r in registry:
        try:
            base = r.abs_path(root).resolve()
        except OSError:
            continue
        if target == base or base in target.parents:
            holding.append((len(base.parts), r))
    return (max(holding, key=lambda pair: pair[0])[1], True) if holding else (None, False)


def _safe_common_dir(start: Path) -> Path | None:
    try:
        return _common_dir(start) if start.exists() else None
    except Exception:  # noqa: BLE001 — git that cannot say leaves the repo to the path rules
        return None


def _unclean(root: Path, head: str, repo: str | None, mine: list[dict[str, Any]], branch: str) -> str | None:
    """Why `head` has no clean code-review round in `repo` (any repo when the push's is unknown);
    None when it has one or a round file of the head is not readable JSON (fail open). A round
    recorded under the pushed branch's name decides alone; only with none does a clean round
    under a guarded dispatch's branch name count."""
    from .. import code_review

    if code_review.unreadable(root, head):
        return None
    if bad := code_review.malformed(root, head):
        return f"a malformed round record ({', '.join(bad)})"
    if code_review.latest(root, branch=branch, head=head, repo=repo) is None:
        others = dict.fromkeys(
            str(d.get("branch")) for d in mine if d.get("branch") and d.get("branch") != branch
        )
        if any(code_review.clean(root, branch=name, head=head, repo=repo) for name in others):
            return None
    return code_review.unclean(root, branch=branch, head=head, repo=repo)


def _code_review_reason(records: list[dict[str, Any]], branch: str, head: str, why: str) -> str:
    build = next(
        (d for d in records if not REVIEW_REASON_RE.search(str(d.get("choice_reason") or ""))), records[0]
    )
    return (
        f"{branch} at {head[:12]} has no clean code-review round on that head: {why}. A push needs a "
        "complete `code-review` round with zero verified blocking findings on the pushed head "
        f'(ADR-0063). Run the `dispatch` action `code-review` with dispatch_id="{build.get("id")}", '
        "and call it again after each return until the round completes. To push anyway, prefix the "
        f"command with {PUSH_OVERRIDE_ENV}=1 (it is logged to the feature)."
    )


_GIT_C_RE = re.compile(r"(?:^|\s)-C\s+(\S+)")
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _push_tokens(command: str) -> list[str]:
    """`command` with any leading `VAR=value` env prefix dropped.

    So `AGENT_PUSH_UNREVIEWED=1 git push` still parses as a git push."""
    toks = shell_tokens(command)
    i = 0
    while i < len(toks) and _ENV_PREFIX_RE.match(toks[i]):
        i += 1
    return toks[i:]


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
    m = _GIT_C_RE.search(command)
    return Path(m.group(1)) if m else Path(cwd)


def _worktree_records(records: list[dict[str, Any]], where: Path) -> list[dict[str, Any]]:
    """Every dispatch whose worktree holds `where`, so a push from a worker's checkout is
    guarded whatever the branch is called. Path-only, like `_repo_for_path`: no git, no
    subprocess, inside the hook's budget."""
    try:
        target = where.expanduser().resolve()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for d in records:
        raw = str(d.get("worktree") or "")
        if not raw:
            continue
        try:
            base = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if target == base or base in target.parents:
            out.append(d)
    return out


def _git_line(cwd: Path, args: list[str]) -> str | None:
    import subprocess

    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2, check=False)
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _current_branch(cwd: Path) -> str:
    name = _git_line(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""
    return "" if name == "HEAD" else name


def _note_unreviewed_push(root: Path, mine: list[dict[str, Any]], branch: str, head: str, why: str) -> None:
    """The override is not silent: it goes in the log of the feature of the pushed repo's dispatch,
    where the next retro reads it, naming the head and why its round is not clean."""
    _note(
        root,
        mine,
        f"pushed {branch} at {head[:12]} with {PUSH_OVERRIDE_ENV}=1 without a clean code-review round: {why}.",
    )


def _note_unlisted_push(root: Path, held: list[dict[str, Any]], why: str) -> None:
    """An override of a push whose refs cannot be listed. Only a push from a dispatch's worktree
    has a feature to log on; any other names no branch to find one by."""
    _note(
        root,
        held,
        f"pushed with {PUSH_OVERRIDE_ENV}=1 a push whose refs push-guard cannot list ({why}), so no "
        "code-review round was checked.",
    )


def _note(root: Path, mine: list[dict[str, Any]], text: str) -> None:
    """Log `text` on the first of `mine`'s features. Fails open like everything else here."""
    try:
        from .. import features as features_mod
        from .. import log as log_mod

        feature_id = next((d.get("feature") for d in mine if d.get("feature") is not None), None)
        if feature_id is None:
            return
        log_mod.append(features_mod.resolve(root, str(feature_id)), "note", text)
    except Exception:  # noqa: BLE001, S110 — a hook never raises; failing open is the contract
        pass
