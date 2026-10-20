"""`pull-warn` and `push-guard`: the two PreToolUse hooks about a registered clone's freshness
and review state. `pull-warn` is advisory (a read against a stale
clone); `push-guard` is a deny (an unreviewed branch pushed to a remote).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .. import sessions
from ..records.narrow import as_dict, dig
from .paths import deny, shell_tokens

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
    "this branch has no `review` dispatch newer than its last commit. /dispatch's "
    '"before the push" step is a review-row dispatch that reads the '
    "branch against the work order's precedents and the repo's testing guidelines; its findings go back to the build worker with `dispatch resume` before the "
    "push. To push anyway, prefix the command with "
    f"{PUSH_OVERRIDE_ENV}=1 (it is logged to the feature)."
)


def push_guard(root: Path, payload: dict[str, Any]) -> str:
    """Require a current review for pushes of dispatched branches or from dispatch worktrees,
    including renamed branches. Other manager branches are outside this guard.

    Accept a finished review-row dispatch after the branch's latest commit, or a review whose
    recorded head equals the pushed head. An older-head review does not qualify.
    AGENT_PUSH_UNREVIEWED=1 overrides and logs a feature note. Fail open on unreadable state so a
    slow Git operation cannot wedge a legitimate push.
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return ""
    command = str(dig(payload, "tool_input", "command") or "")
    if not _is_git_push(command):
        return ""
    where = _push_cwd(command, str(payload.get("cwd") or root))
    branch = _push_branch(command, str(payload.get("cwd") or root))
    from .. import dispatch as dispatch_mod

    all_records = dispatch_mod.load_all(root)
    records = [d for d in all_records if branch and str(d.get("branch") or "") == branch]
    records += [d for d in _worktree_records(all_records, where) if d not in records]
    if not records:
        return ""
    if not branch:
        # a detached head in a worker's worktree: nothing to date a review against
        return deny(PUSH_GUARD_REASON)
    if PUSH_OVERRIDE_ENV in command:
        _note_unreviewed_push(root, records, branch)
        return ""
    last_commit = _branch_last_commit(where, branch)
    for d in records:
        if not REVIEW_REASON_RE.search(str(d.get("choice_reason") or "")):
            continue
        finished = str(d.get("finished") or "")
        if finished and (last_commit is None or finished > last_commit):
            return ""
    if _reviewed_head(all_records, records, where, branch):
        return ""
    return deny(PUSH_GUARD_REASON)


def _reviewed_head(
    all_records: list[dict[str, Any]], records: list[dict[str, Any]], where: Path, branch: str
) -> bool:
    """A review on its own branch and worktree, cut from the build branch, as /dispatch runs it.
    Neither its branch nor its worktree matches the push, so it counts only on
        proof that it read the pushed commit: a `done` review-row dispatch, in the same repo as a
        guarded record, whose `start_sha` is the pushed head. A branch name or a finish time is not
        proof: a review that started at H1 and finished after the branch moved to H2 did not read
        H2. `start_sha` is set at the first attempt and kept across resumes, so a review resumed
        after the branch moved does not count either; that errs toward the override."""
    head = _git_line(where, ["rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"])
    repos = {str(d.get("repo")) for d in records if d.get("repo")}
    if not head or not repos:
        return False
    return any(
        REVIEW_REASON_RE.search(str(d.get("choice_reason") or ""))
        and d.get("state") == "done"
        and str(d.get("repo") or "") in repos
        and str(d.get("start_sha") or "") == head
        for d in all_records
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


def _push_branch(command: str, cwd: str) -> str:
    """The branch being pushed.

    The last non-flag token when the command names a remote and a ref
    (`git push origin agent/016`). Otherwise the checked-out branch of the cwd (or the `-C`
    path). `HEAD` and a detached checkout read as no branch, and the hook falls open."""
    toks = [t for t in _push_tokens(command) if not t.startswith("-")]
    if "push" in toks:
        after = toks[toks.index("push") + 1 :]
        if len(after) >= 2:
            ref = after[1].split(":")[-1].removeprefix("refs/heads/")
            if ref and ref != "HEAD":
                return ref
    return _current_branch(_push_cwd(command, cwd))


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


def _branch_last_commit(cwd: Path, branch: str) -> str | None:
    """The branch tip's commit time, ISO-8601 with an offset, for comparison against a
    dispatch's `finished`. `None` when git cannot say. That makes any finished review count:
    fail open."""
    return _git_line(cwd, ["log", "-1", "--format=%cI", branch])


def _note_unreviewed_push(root: Path, records: list[dict[str, Any]], branch: str) -> None:
    """The override is not silent: it goes in the feature's log, where the next retro reads it.

    Fails open like everything else here."""
    try:
        from .. import features as features_mod
        from .. import log as log_mod

        feature_id = next((d.get("feature") for d in records if d.get("feature") is not None), None)
        if feature_id is None:
            return
        f = features_mod.resolve(root, str(feature_id))
        log_mod.append(
            f,
            "note",
            f"pushed {branch} with {PUSH_OVERRIDE_ENV}=1: no review-row dispatch newer than its last commit.",
        )
    except Exception:  # noqa: BLE001, S110 — a hook never raises; failing open is the contract
        pass
