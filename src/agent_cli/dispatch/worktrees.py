"""The git worktree a dispatch runs in: create it, reuse a feature+repo one, run the repo's
setup commands in it, read commits and files back out of it, and remove finished worktrees."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .. import features as features_mod
from .. import repos as repos_mod
from ..core import AgentError, ExternalFailure
from ..records.narrow import as_dict, as_dict_or_none, as_list
from . import branch_heads, telemetry_events
from .state import (
    ATTENTION_STATES,
    TERMINAL_STATES,
    WORKTREE_SETUP_TIMEOUT_S,
    for_feature,
    load,
    save,
)

# ------------------------------------------------------------------ worktrees (A2)


def ensure_worktree(
    root: Path, repo: repos_mod.Repo, path: Path, branch: str, base: str, *, resume: bool = False
) -> dict[str, Any]:
    """Create the worktree in the main checkout, or reuse it when it is already registered
    (a resume). A fresh dispatch starts its branch from origin's exact head when origin has one
    (`branch_heads.fresh_branch`); a resume that recreates a removed worktree checks its local
    branch out as it is. `worktree_s` is how long that took, `git worktree add` included."""
    started = time.monotonic()
    main = repo.abs_path(root)
    if not (main / ".git").exists():
        raise AgentError(f"{repo.short} is not cloned at {main}; run `agent repos sync {repo.short}`")
    existing = {Path(w["path"]).resolve(): w for w in repos_mod.worktrees(main)}
    if path.resolve() in existing:
        return {
            "path": str(path),
            "branch": existing[path.resolve()].get("branch"),
            "created": False,
            "worktree_s": _since(started),
        }
    if path.exists():
        raise AgentError(
            f"{path} exists but is not a worktree of {repo.short}; remove it or pick another --branch"
        )
    if any(w.get("branch") == branch for w in existing.values()):
        raise AgentError(f"branch {branch} is already checked out in another worktree; pass --branch")
    prepare = branch_heads.resume_branch if resume else branch_heads.fresh_branch
    target, branch_exists = prepare(main, branch, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .allocation import git_admin

    with git_admin(root, repo.short):
        if branch_exists:
            repos_mod.run_git(["worktree", "add", str(path), branch], main)
        else:
            repos_mod.run_git(["worktree", "add", "-b", branch, str(path), target], main)
    return {
        "path": str(path),
        "branch": branch,
        "base": target,
        "created": True,
        "worktree_s": _since(started),
    }


def _since(started: float) -> float:
    return round(time.monotonic() - started, 3)


class AttemptClock:
    """Telemetry for an attempt's start: `dispatch.launch` when made, then
    `dispatch.setup` once the worktree is ready. That is at once for a worktree with no setup
    pending, or after `env` has timed the repo's `dispatch.setup` commands."""

    def __init__(self, root: Path, d: dict[str, Any]) -> None:
        self.root, self.d = root, dict(d)
        telemetry_events.launch(root, self.d)
        answered = as_dict(d.get("answered_question"))
        question = {"question_id": str(answered["id"])} if answered.get("attempt") == d.get("attempt") else {}
        telemetry_events.transition(root, self.d, "model-active", **question)
        if not d.get("setup_pending"):
            self._setup(None)

    def env(self, run: Any, *args: Any) -> Any:
        started = time.monotonic()
        out = run(*args)
        self._setup(_since(started))
        return out

    def _setup(self, env_s: float | None) -> None:
        timing = as_dict_or_none(self.d.get("setup_timing"))
        if timing is not None and timing.get("attempt") == self.d.get("attempt"):
            telemetry_events.setup(self.root, self.d, timing, env_s)


def launch_base(
    root: Path,
    repo: repos_mod.Repo,
    base: str | None,
    *,
    row: str | None = None,
    target: str | None = None,
) -> str:
    """The base a new dispatch starts from, as an immutable SHA where it can be: an explicit
    local `base` (a branch, a tag, `main`) is resolved to its commit now, so a later move of that
    ref cannot change what the dispatch was based on. A review's `target` supplies the pinned
    checkout commit when `base` is omitted. An explicit `origin/` base is fetched at launch
    instead. A dispatch into this harness
    itself starts from locally landed main, pinned to its SHA: `agent land` moves local main, and
    pushes wait for the owner, so `origin/main` trails it. A
    product repo keeps `origin/<default branch>`, unless its clone has no `origin`: a local-only
    repo starts from its local default branch, pinned the same way."""
    from ..worktree import main_checkout

    main = repo.abs_path(root)
    if base is None and row == "review" and target:
        sha = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{target}^{{commit}}"], main)
        if not sha:
            raise AgentError(f"review target {target!r} is not a commit in {repo.name}; fetch it first")
        return sha.strip()
    if base:
        if base.startswith("origin/"):
            return base
        sha = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], main)
        if not sha:
            raise AgentError(f"base {base!r} is not a commit in {repo.name}; land or fetch it first")
        return sha.strip()
    try:
        harness = main_checkout(main).resolve() == main_checkout(root).resolve()
    except AgentError, OSError:
        harness = False
    if not harness and repos_mod.has_origin(main) is not False:
        return f"origin/{repo.default_branch}"
    sha = repos_mod.git_or_none(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{repo.default_branch}^{{commit}}"], main
    )
    if not sha:
        which = "a harness dispatch" if harness else "a repo with no origin"
        raise AgentError(
            f"local {repo.default_branch} does not resolve in {repo.name}; {which} starts from it"
        )
    return sha.strip()


def reusable_worktree(
    root: Path,
    feat: features_mod.Feature,
    repo: repos_mod.Repo,
    tag: str | None = None,
    branch: str | None = None,
    *,
    excluded: set[str] | None = None,
) -> Path | None:
    """Reuse a feature/repo worktree to retain installed dependencies only when its last dispatch is
    finished, not in an attention state, and the tree is clean. Prefer the eligible path finished
    most recently; None requires a fresh worktree.

    Consider all recorded paths, but tagged dispatches use only their own machine suffix (see
    ADR-0036). Keep an unlanded artifact on its branch: only a dispatch targeting that same branch
    may continue there. Never replace a worktree the owner is expected to resume.
    """
    own = re.compile(rf"-{re.escape(tag)}-\d{{2,}}$") if tag else None
    latest_by_path: dict[Path, dict[str, Any]] = {}
    for d in for_feature(root, feat):
        if d.get("repo") != repo.name:
            continue
        wt = Path(d["worktree"])
        if str(wt) in (excluded or set()):
            continue
        if own is not None and not own.search(wt.name):
            continue
        prev = latest_by_path.get(wt)
        if prev is None or (d.get("created") or "") > (prev.get("created") or ""):
            latest_by_path[wt] = d
    best: Path | None = None
    best_finished = ""
    for wt, last in latest_by_path.items():
        if last["state"] not in TERMINAL_STATES or last["state"] in ATTENTION_STATES:
            continue
        if last.get("worktree_removed") or not wt.exists():
            continue
        if worktree_dirty(wt) or (last.get("branch") != branch and artifact_open(root, repo, last)):
            continue
        finished = str(last.get("finished") or last.get("created") or "")
        if best is None or finished > best_finished:
            best, best_finished = wt, finished
    return best


def artifact_open(root: Path, repo: repos_mod.Repo, d: dict[str, Any]) -> bool:
    """Whether `d`'s branch still holds work its builder owns: commits not yet on local main, and
    the dispatch not abandoned. Such a worktree is not reused, so a review gets its own checkout
    and findings can resume the builder. The branch tip decides: it
    is released when it is on main or is the exact tip a landing recorded (a squash landing puts a
    new commit on main, so ancestry alone would hold the worktree forever); a builder resumed after
    its branch landed, with new commits, still owns it."""
    if d.get("state") == "abandoned":
        return False
    main = repo.abs_path(root)
    tip = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"refs/heads/{d.get('branch')}"], main)
    if not tip:
        return False
    landed = as_dict(d.get("integration"))
    if landed.get("state") == "landed" and landed.get("tip") == tip.strip():
        return False  # exactly what landed, squash or merge included; a later commit is not
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tip.strip(), f"refs/heads/{repo.default_branch}"],
        cwd=main,
        capture_output=True,
        check=False,
    )
    return merged.returncode != 0


def reuse_worktree(root: Path, repo: repos_mod.Repo, path: Path, branch: str, base: str) -> dict[str, Any]:
    """Parallel to `ensure_worktree`: the same branch-head choice, but checks out `branch` inside
    an already-clean worktree instead of creating a new directory, so whatever the setup commands
    produced there (`node_modules`, most of the time) survives. A dirty, unregistered or
    elsewhere-occupied checkout is refused untouched."""
    started = time.monotonic()
    main = repo.abs_path(root)
    occupied = branch_heads.reusable_checkout(main, path, branch)
    # An existing branch is checked out as it is, never reset to `base`: `checkout -B` on
    # a review dispatched with `--branch` onto the build branch reset that branch to
    # origin/main and left the PR's commits dangling. A new branch starts from
    # origin's head for it when origin has one, else from `base`.
    target, branch_exists = branch_heads.fresh_branch(main, branch, base, occupied=occupied)
    if branch_exists:
        repos_mod.run_git(["checkout", "--quiet", branch], path)
    else:
        repos_mod.run_git(["checkout", "-b", branch, target], path)
    return {
        "path": str(path),
        "branch": branch,
        "base": target,
        "created": False,
        "reused": True,
        "worktree_s": _since(started),
    }


def run_worktree_setup(
    root: Path, repo: repos_mod.Repo, path: Path, env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Run the repo's `dispatch.setup` commands in a freshly created worktree.

    A new worktree has no installed dependencies, so the repo's build, test and lint tooling and
    its pre-commit hook are simply absent. That makes the entire verification section of a work
    order unreachable, and is not something the work order can explain away.

    Deliberately best-effort: a failure is recorded and reported, not raised. The worker can
    still run, still retry the install itself, and still return `blocked` with a better
    description of the problem than we could produce here. Only runs on a fresh worktree.
    A resume reuses the existing one and its already-populated install.
    """
    from .. import procs

    out: list[dict[str, Any]] = []
    for cmd in as_list(repo.dispatch.get("setup")):
        started = time.monotonic()
        try:
            # Run setup in a bounded group with no stdin. Killing only a timeout's shell can leave
            # postinstall downloads and machine-wide locks behind.
            proc = procs.run_bounded(  # noqa: S604 — agent.yaml setup lines are shell by design, preflighted against the worker rules
                str(cmd),
                shell=True,
                cwd=path,
                timeout=WORKTREE_SETUP_TIMEOUT_S,
                env=env,
            )
            code, tail = proc.returncode, (proc.stderr or proc.stdout or "").strip()[-400:]
        except subprocess.TimeoutExpired as exc:
            suffix = "; process-tree cleanup unconfirmed" if isinstance(exc, procs.CleanupUnconfirmed) else ""
            code, tail = -1, f"timed out after {WORKTREE_SETUP_TIMEOUT_S}s{suffix}"
        except OSError as exc:
            code, tail = -1, str(exc)
        entry = {"cmd": str(cmd), "exit": code, "seconds": round(time.monotonic() - started, 1)}
        if code != 0:
            entry["error"] = tail
            sys.stderr.write(f"note: worktree setup `{cmd}` exited {code} in {path}: {tail}\n")
        out.append(entry)
    return out


def git_commits(worktree: Path, base: str) -> list[dict[str, str]]:
    """The branch's own commits since `base`, newest first. `--first-parent` leaves out the
    commits a merge of main brought in: they are main's, not this dispatch's (a merge-main round listed every merged-in commit as the worker's). A merge commit carries `merge: "1"`."""
    out = repos_mod.git_or_none(
        ["log", "--first-parent", "--format=%H%x09%P%x09%s", f"{base}..HEAD"], worktree
    )
    commits: list[dict[str, str]] = []
    for line in (out or "").splitlines():
        sha, _, rest = line.partition("\t")
        parents, _, subject = rest.partition("\t")
        if sha:
            commit = {"sha": sha, "subject": subject}
            if len(parents.split()) > 1:
                commit["merge"] = "1"
            commits.append(commit)
    return commits


def git_files(worktree: Path, base: str) -> list[str]:
    files: list[str] = []
    for line in (
        repos_mod.git_or_none(["diff", "--name-only", f"{base}..HEAD"], worktree) or ""
    ).splitlines():
        if line.strip() and line.strip() not in files:
            files.append(line.strip())
    for line in (repos_mod.git_or_none(["status", "--porcelain"], worktree) or "").splitlines():
        name = line[3:].strip()
        if name and name not in files:
            files.append(name)
    return files


def worktree_dirty(worktree: Path) -> bool:
    out = repos_mod.git_or_none(["status", "--porcelain"], worktree)
    return bool(out and out.strip())


def _untracked_files(worktree: Path) -> set[str]:
    """Unignored files Git would lose on a forced worktree removal."""
    output = repos_mod.run_git(["ls-files", "--others", "--exclude-standard", "-z"], worktree)
    return {name for name in output.split("\0") if name}


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matches_collected(worktree: Path, name: str, digest: object) -> bool:
    path = worktree / name
    if path.is_symlink() or not path.is_file() or worktree.resolve() not in path.resolve().parents:
        return False
    return _file_hash(path) == digest


def collect_files(root: Path, *, dispatch_id: str, paths: list[str]) -> dict[str, Any]:
    """Copy selected untracked worker files into this dispatch's private handoff artifacts."""
    if not paths:
        raise AgentError("pass at least one --path to collect-files")
    d = load(root, dispatch_id)
    wt = Path(d["worktree"])
    if d.get("worktree_removed") or not wt.is_dir():
        raise AgentError(f"dispatch {dispatch_id} has no worktree to collect from")
    feat = features_mod.resolve(root, str(d["feature"]))
    artifact_dir = feat.dir / "handoffs" / f"{dispatch_id}-files"
    from .allocation import GUARD_TIMEOUT_S
    from .guards import area_guard

    with area_guard(root, f"dispatch-records:{d['short']}", timeout=GUARD_TIMEOUT_S):
        d = load(root, dispatch_id)
        if d["state"] in ("queued", "running"):
            raise AgentError("collect-files requires a stopped worker")
        available = _untracked_files(wt)
        selected: list[tuple[str, Path, bytes, str]] = []
        for name in paths:
            relative = Path(name)
            source = wt / relative
            if (
                name not in available
                or relative.is_absolute()
                or ".." in relative.parts
                or source.is_symlink()
            ):
                raise AgentError(f"{name!r} is not an unignored regular file in this worktree")
            if not source.is_file() or wt.resolve() not in source.resolve().parents:
                raise AgentError(f"{name!r} is not an unignored regular file in this worktree")
            contents = source.read_bytes()
            digest = hashlib.sha256(contents).hexdigest()
            selected.append((name, artifact_dir / digest / relative, contents, digest))
        for name, destination, contents, digest in selected:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.is_symlink() or _file_hash(destination) != digest:
                    raise AgentError(f"collected artifact already exists with different content: {name}")
            else:
                with destination.open("xb") as stream:
                    stream.write(contents)
        collected = dict(d.get("collected_files") or {})
        for name, _, _, digest in selected:
            collected[name] = digest
        d["collected_files"] = collected
        save(root, d)
    return {
        "id": dispatch_id,
        "artifacts": [str(destination.relative_to(root)) for _, destination, _, _ in selected],
    }


def cleanup(
    root: Path,
    *,
    feature: str | None = None,
    dispatch_id: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Remove a feature's worktrees or one dispatch's tree. Feature scope refuses while a dispatch
    runs; dispatch scope checks all users of that physical tree. Keeps active trees, leases and
    uncollected files even with force. Without `force`, owner waits, real changes and unresolved
    branches also hold removal; generated output and collected files do not. Branches are always kept.
    With `dry_run` it
    decides exactly as a real run and returns the removals as `would_remove`, changing nothing.

    Worktree reuse (1.5) means more than one dispatch record can point at the same physical
    path. Without deduping first, the second `git worktree remove` on an already-gone path
    would either raise or (worse) get reported as a second, confusing skip for a worktree
    already removed. So records are grouped by path first, and each path is removed and
    reported exactly once.
    """
    if (feature is None) == (dispatch_id is None):
        raise AgentError("cleanup needs `feature` or `dispatch_id`, exclusively")
    selected = load(root, dispatch_id) if dispatch_id is not None else None
    feat = features_mod.resolve(root, str(selected["feature"]) if selected is not None else str(feature))
    registry = repos_mod.load_registry(root)
    ds = for_feature(root, feat)
    if selected is not None:
        ds = [d for d in ds if Path(d["worktree"]).resolve() == Path(selected["worktree"]).resolve()]
    running = [d["id"] for d in ds if d["state"] in ("queued", "running")]
    if running:
        raise AgentError(f"dispatch(es) still running: {', '.join(running)}; cancel or collect them first")
    groups: dict[Path, list[dict[str, Any]]] = {}
    for d in ds:
        groups.setdefault(Path(d["worktree"]), []).append(d)
    removed: list[str] = []
    would_remove: list[str] = []
    skipped: list[dict[str, str]] = []
    branches: list[str] = []
    import os

    from .allocation import GUARD_TIMEOUT_S, git_admin
    from .guards import area_guard
    from .holdings import cleanup_take, refuse_busy, rollback
    from .reverify import finish_stuck
    from .state import load_all
    from .verification_gate import requires_lease

    if not dry_run:
        finish_stuck(root, ds)
    sid = f"cleanup:{feat.id}:{os.getpid()}"
    for wt, original in groups.items():
        from .. import tidy

        initial_holds = tidy.active_holds(root, wt, feat.id, session_id=session_id)
        with area_guard(root, f"dispatch-records:{original[-1]['short']}", timeout=GUARD_TIMEOUT_S):
            group = [d for d in load_all(root) if Path(d["worktree"]).resolve() == wt.resolve()]
            if not group:
                continue
            if any(d["state"] in ("queued", "running") or requires_lease(root, d) for d in group):
                raise AgentError("worktree is active or cleanup is unconfirmed; cancel or collect first")
            if reasons := tidy.dispatch_holds(root, group, force=force):
                skipped.append({"id": group[-1]["id"], "worktree": str(wt), "reason": "; ".join(reasons)})
                continue
            refuse_busy(root, original[-1]["short"], wt)
            rp = registry.find(group[-1]["repo"])
            if rp is None or not wt.exists():
                continue
            taken = cleanup_take(root, original[-1]["short"], wt, sid)
        try:
            untracked = _untracked_files(wt) | set(tidy.ignored_files(wt))
            collected = {
                name: digest for gd in group for name, digest in as_dict(gd.get("collected_files")).items()
            }
            uncollected = sorted(
                name
                for name in untracked
                if not tidy.generated(name)
                and (name not in collected or not _matches_collected(wt, name, collected[name]))
            )
            reason = (
                "uncollected files: "
                + ", ".join(uncollected)
                + " (collect them with `dispatch` action `collect-files`)"
                if uncollected
                else None
            )
            safe_collected = {
                name
                for name in untracked
                if name in collected and _matches_collected(wt, name, collected[name])
            }
            active = initial_holds + tidy.active_holds(
                root, wt, feat.id, ignore_holder=sid, session_id=session_id
            )
            if reason is None and active:
                reason = "; ".join(active)
            if reason is None and not force and tidy.changes(wt, safe_collected):
                reason = "dirty; pass force"
            if (
                reason is None
                and not force
                and not tidy.resolved(
                    root, rp.abs_path(root), str(group[-1]["branch"]), rp.default_branch, group
                )
            ):
                reason = tidy.UNRESOLVED_BRANCH
            with area_guard(root, f"dispatch-records:{original[-1]['short']}", timeout=GUARD_TIMEOUT_S):
                if reason is None and any(
                    load(root, gd["id"])["record_revision"] != gd["record_revision"] for gd in group
                ):
                    reason = "its dispatch record changed while cleanup checked it; run cleanup again"
            if reason is not None:
                skipped.append({"id": group[-1]["id"], "worktree": str(wt), "reason": reason})
                continue
            if dry_run:
                would_remove.append(str(wt))
                for gd in group:
                    if gd["branch"] not in branches:
                        branches.append(gd["branch"])
                continue
            main = rp.abs_path(root)
            args = ["worktree", "remove", "--force", str(wt)]
            try:
                with git_admin(root, rp.short):
                    from ..worktree import unlink_links

                    active = tidy.active_holds(root, wt, feat.id, ignore_holder=sid, session_id=session_id)
                    if active:
                        skipped.append(
                            {"id": group[-1]["id"], "worktree": str(wt), "reason": "; ".join(active)}
                        )
                        continue
                    unlink_links(wt)
                    repos_mod.run_git(args, main)
                    repos_mod.git_or_none(["worktree", "prune"], main)
            except ExternalFailure as exc:
                skipped.append({"id": group[-1]["id"], "worktree": str(wt), "reason": exc.message})
                continue
            removed.append(str(wt))
            with area_guard(root, f"dispatch-records:{original[-1]['short']}", timeout=GUARD_TIMEOUT_S):
                for gd in group:
                    current = load(root, gd["id"])
                    current["worktree_removed"] = True
                    save(root, current)
                    if gd["branch"] not in branches:
                        branches.append(gd["branch"])
        finally:
            rollback(root, taken)
    if dry_run:
        return {
            "feature": feat.id,
            "dry_run": True,
            "would_remove": would_remove,
            "skipped": skipped,
            "branches_kept": branches,
        }
    return {"feature": feat.id, "removed": removed, "skipped": skipped, "branches_kept": branches}
