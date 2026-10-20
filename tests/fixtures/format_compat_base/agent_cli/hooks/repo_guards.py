# ruff: noqa
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Any
from .. import sessions
from ..records.narrow import as_dict, dig
from .paths import deny, shell_tokens

PULLED_KEY = "pulled"
PULL_WARNED_KEY = "pull_warned"
PULL_WARN_TOOLS = ("Read", "Grep", "Glob")


def _repo_for_path(root: Path, file_path: str) -> str | None:
    if not file_path:
        return None
    from .. import repos as repos_mod

    try:
        target = Path(file_path).expanduser()
        target = target if target.is_absolute() else root / target
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
                "additionalContext": f'{short} has not been pulled in this session; what you read may be stale. Run the repos_sync tool with pull=True (name="{short}") before reporting anything from it.',
            }
        }
    )


PUSH_OVERRIDE_ENV = "AGENT_PUSH_UNREVIEWED"
REVIEW_ROW = "review"
REVIEW_REASON_RE = re.compile("^\\s*review(?=[:,]|\\s+row\\b)|(?:^|;\\s*)rubric row review:", re.I)
PUSH_GUARD_REASON = f"""this branch has no `review` dispatch newer than its last commit. /dispatch's "before the push" step is a review-row dispatch that reads the branch against the work order's precedents and the repo's testing guidelines; its findings go back to the build worker with `dispatch resume` before the push. To push anyway, prefix the command with {PUSH_OVERRIDE_ENV}=1 (it is logged to the feature)."""


def push_guard(root: Path, payload: dict[str, Any]) -> str:
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
    head = _git_line(where, ["rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"])
    repos = {str(d.get("repo")) for d in records if d.get("repo")}
    if not head or not repos:
        return False
    return any(
        (
            REVIEW_REASON_RE.search(str(d.get("choice_reason") or ""))
            and d.get("state") == "done"
            and (str(d.get("repo") or "") in repos)
            and (str(d.get("start_sha") or "") == head)
            for d in all_records
        )
    )


_GIT_C_RE = re.compile("(?:^|\\s)-C\\s+(\\S+)")
_ENV_PREFIX_RE = re.compile("^[A-Za-z_][A-Za-z0-9_]*=")


def _push_tokens(command: str) -> list[str]:
    toks = shell_tokens(command)
    i = 0
    while i < len(toks) and _ENV_PREFIX_RE.match(toks[i]):
        i += 1
    return toks[i:]


_GIT_FLAGS_WITH_ARG = ("-C", "--git-dir", "--work-tree", "--namespace", "-c")


def _is_git_push(command: str) -> bool:
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
    return _git_line(cwd, ["log", "-1", "--format=%cI", branch])


def _note_unreviewed_push(root: Path, records: list[dict[str, Any]], branch: str) -> None:
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
    except Exception:
        pass
