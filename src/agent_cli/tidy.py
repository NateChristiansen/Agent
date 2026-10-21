"""Finished-checkout cleanup, shared by close, doctor and tidy.

Dry runs read only. Automatic removals preserve active checkouts, leases, real changes and
unresolved branches. Landing evidence binds the branch tip, since a squash is not ancestry.
Feature close keeps branches; explicit tidy apply deletes only resolved, owned branches.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import core, features, leases, links, repos, sessions, worktree
from . import land_observe as obs
from .records.narrow import as_dict, dig
from .session_liveness import record_live

UNRESOLVED_BRANCH = (
    "unresolved branch: its commits are not on the default branch; land them (`harness` action "
    "`land` in the harness repo, or the repo's own merge) or remove the worktree anyway with "
    "`dispatch` action `cleanup` with force set, which keeps the branch"
)
COPY_HINT = "possibly copied: pass origins next time"


def generated(name: str) -> bool:
    """Only untracked caches and test output are disposable; tracked changes always count."""
    if set(Path(name).parts) & {
        ".venv",
        "node_modules",
        ".pytest_cache",
        "__pycache__",
        ".ruff_cache",
        ".mypy_cache",
        ".pyright",
    }:
        return True
    first = name.split("/", 1)[0]
    if re.fullmatch(r"\.bt[0-9]+", first):
        return "/" in name
    return first in (".pytest-tmp", "pytest-temproot")


def ignored_files(path: Path) -> list[str]:
    """Inventory ignored work without descending into disposable dependency caches."""
    output = repos.run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"], path
    )
    files: list[str] = []
    pending = [name for name in output.split("\0") if name]
    while pending:
        name = pending.pop()
        if generated(name):
            continue
        entry = path / name
        if links.is_link(entry):
            continue
        if entry.is_dir():
            pending.extend(child.relative_to(path).as_posix() for child in entry.iterdir())
        else:
            files.append(name)
    return files


def harness_exempt(path: Path, harness_root: Path) -> set[str]:
    """A harness checkout's regenerable files, and its `.agent` when that links to the shared state."""
    exempt = {p.as_posix() for p in (*core.GENERATED_VIEWS.values(), *core.REGENERABLE_STATE)}
    state = core.agent_root(path)
    if links.is_link(state) and state.resolve() == core.agent_root(harness_root).resolve():
        exempt.add(state.name)
    return exempt


def changes(path: Path, collected: set[str] | None = None, *, harness_root: Path | None = None) -> list[str]:
    output = repos.run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
    rows = iter(output.split("\0"))
    real: list[str] = []
    for row in rows:
        if not row:
            continue
        code, name = row[:2], row[3:]
        names = [name]
        if "R" in code or "C" in code:
            if source := next(rows, None):
                names.append(source)
        if code == "??" and (generated(name) or name in (collected or set())):
            continue
        real.extend(names)
    real.extend(name for name in ignored_files(path) if name not in (collected or set()))
    if harness_root is None:
        return sorted(set(real))
    exempt = harness_exempt(path, harness_root)
    state = core.agent_root(path)
    if state.name not in exempt and state.exists() and not links.is_link(state):
        pending = [Path(state)]
        while pending:
            for entry in pending.pop().iterdir():
                if entry.is_dir() and not links.is_link(entry):
                    pending.append(entry)
                else:
                    real.append(entry.relative_to(path).as_posix())
    return sorted({name for name in real if name not in exempt})


def process_cwds() -> list[tuple[int, str]]:
    """Current cwd inventory without command lines or environment values."""
    rows: list[tuple[int, str]] = []
    if sys.platform == "win32":
        import psutil

        for proc in psutil.process_iter():
            try:
                rows.append((proc.pid, proc.cwd()))
            except psutil.NoSuchProcess, psutil.AccessDenied:
                continue
        return rows
    if sys.platform.startswith("linux"):
        rows = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    rows.append((int(entry.name), str((entry / "cwd").resolve(strict=True))))
                except FileNotFoundError, PermissionError:
                    continue
        return rows
    result = subprocess.run(
        ["lsof", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise core.AgentError("process cwd inventory unavailable")
    rows = []
    pid = 0
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            rows.append((pid, line[1:]))
    return rows


def _inside(cwd: object, path: Path) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    candidate = Path(cwd).resolve()
    return candidate == path.resolve() or path.resolve() in candidate.parents


def active_holds(
    root: Path,
    path: Path,
    feature: int | None,
    *,
    cwds: list[tuple[int, str]] | None = None,
    ignore_holder: str | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Hold only active paths and live tree leases, excluding the caller's own leases."""
    from .dispatch.allocation import git_admin_short, worktree_area
    from .dispatch.notice_producers import caller_session

    records = sessions.all_records(root)
    reasons = [
        f"live session {r.get('session_id')}"
        for r in records
        if _inside(r.get("cwd"), path)
        and dig(r, "activity", "state") != "ended"
        and record_live(r, core.is_alive)
    ]
    reasons += [
        f"process cwd {pid}" for pid, cwd in (process_cwds() if cwds is None else cwds) if _inside(cwd, path)
    ]
    holders = {r.get("session_id"): r for r in records}
    ignored = {caller_session(root, session_id), ignore_holder}
    for lease in leases.list_all(root):
        sid = lease.get("session_id")
        if sid in ignored:
            continue
        holder = holders.get(sid) or lease
        if dig(holder, "activity", "state") == "ended" or not record_live(holder, core.is_alive):
            continue
        area = str(lease.get("area") or "")
        key = lease.get("worktree_key")
        on_tree = _inside(key, path) or _inside(holder.get("cwd"), path)
        if not key and area.startswith("worktree:"):
            main = worktree.main_checkout(path)
            on_tree = on_tree or area == worktree_area(git_admin_short(root, main), path)
        if on_tree:
            reasons.append(f"lease {area}")
    return reasons


class ResolutionContext:
    """Landing observations shared by one inventory or run, never across operations."""

    def __init__(self, root: Path, records: list[dict[str, Any]] | None = None):
        self.root = root
        self._records = records
        self._journals: dict[Path, list[dict[str, Any]]] = {}
        self._ancestors: dict[tuple[Path, str, str], bool] = {}
        self._trees: dict[tuple[Path, str], str | None] = {}
        self._main_entries: dict[tuple[Path, str], dict[str, obs.Entry]] = {}
        self._refs: dict[Path, dict[str, str | None]] = {}
        self._landed_tips: dict[tuple[Path, str], set[str]] = {}
        self._copy_hints: dict[tuple[Path, str, str], bool] = {}
        self._observers: dict[Path, obs.Git] = {}

    def refresh(self, main: Path, default: str) -> None:
        """Keep apply's locked recheck fresh if the default or journal target moved."""
        refs = {
            ref: repos.git_or_none(["rev-parse", "--verify", f"{ref}^{{commit}}"], main)
            for ref in (f"refs/heads/{default}", "main")
        }
        if main in self._refs and self._refs[main] != refs:
            self._ancestors = {key: value for key, value in self._ancestors.items() if key[0] != main}
            self._trees = {key: value for key, value in self._trees.items() if key[0] != main}
            self._main_entries = {key: value for key, value in self._main_entries.items() if key[0] != main}
            self._landed_tips = {key: value for key, value in self._landed_tips.items() if key[0] != main}
            self._copy_hints = {key: value for key, value in self._copy_hints.items() if key[0] != main}
        self._refs[main] = refs

    @property
    def records(self) -> list[dict[str, Any]]:
        from .dispatch import state

        if self._records is None:
            self._records = state.load_all(self.root)
        return self._records

    def journals(self, main: Path) -> list[dict[str, Any]]:
        from . import land_journal

        if main not in self._journals:
            self._journals[main] = land_journal.every(main)
        return self._journals[main]

    def ancestor(self, main: Path, candidate: str, ref: str) -> bool:
        key = (main, candidate, ref)
        if key not in self._ancestors:
            self._ancestors[key] = (
                repos.git_or_none(["merge-base", "--is-ancestor", candidate, ref], main) is not None
            )
        return self._ancestors[key]

    def tree(self, main: Path, candidate: str) -> str | None:
        key = (main, candidate)
        if key not in self._trees:
            self._trees[key] = repos.git_or_none(["rev-parse", f"{candidate}^{{tree}}"], main)
        return self._trees[key]

    def observer(self, main: Path) -> obs.Git:
        """One hardened git per checkout: building it reads config, twice on Windows."""
        if main not in self._observers:
            self._observers[main] = obs.Git(main)
        return self._observers[main]

    def main_entries(self, main: Path, default: str, git: obs.Git) -> dict[str, obs.Entry]:
        key = (main, default)
        if key not in self._main_entries:
            self._main_entries[key] = obs.tree_entries(git, f"refs/heads/{default}")
        return self._main_entries[key]

    def landed_tips(self, main: Path, default: str) -> set[str]:
        key = (main, default)
        if key not in self._landed_tips:
            output = repos.run_git(
                ["log", "--format=%(trailers:key=Landed-tip,valueonly)", f"refs/heads/{default}"], main
            )
            self._landed_tips[key] = {
                match[1]
                for line in output.splitlines()
                if (match := re.fullmatch(r"\S+ ([0-9a-f]{40}|[0-9a-f]{64})(?: origin)?", line))
            }
        return self._landed_tips[key]

    def possibly_copied(self, main: Path, branch: str, default: str) -> bool:
        tip = repos.git_or_none(["rev-parse", "--verify", f"{branch}^{{commit}}"], main)
        if not tip:
            return False
        key = (main, tip.strip(), default)
        if key not in self._copy_hints:
            output = repos.git_or_none(["cherry", f"refs/heads/{default}", tip.strip()], main) or ""
            self._copy_hints[key] = any(line.startswith("- ") for line in output.splitlines())
        return self._copy_hints[key]


def resolved(
    root: Path,
    main: Path,
    branch: str,
    default: str = "main",
    records: list[dict[str, Any]] | None = None,
    *,
    context: ResolutionContext | None = None,
) -> bool:
    context = context if context is not None else ResolutionContext(root, records)
    tip = repos.git_or_none(["rev-parse", "--verify", f"{branch}^{{commit}}"], main)
    if not tip:
        return False
    tip = tip.strip()
    if context.ancestor(main, tip, f"refs/heads/{default}"):
        return True
    if tip in context.landed_tips(main, default):
        return True
    tree = context.tree(main, tip)
    from .dispatch import state

    for d in context.records if records is None else records:
        if d.get("branch") != branch:
            continue
        integration = as_dict(d.get("integration"))
        if integration.get("state") == "landed" and integration.get("tip") == tip:
            return True
        # Old mark_landed records name the journal in actioned.note, not a tip.
        if state.is_actioned(d) and dig(d, "actioned", "action") == "landed":
            ident = dig(d, "actioned", "note")
            for journal in context.journals(main):
                if journal.get("id") == ident and _journal_landed(main, journal, branch, tip, tree, context):
                    return True
    if any(_journal_landed(main, j, branch, tip, tree, context) for j in context.journals(main)):
        return True
    base = repos.git_or_none(["merge-base", f"refs/heads/{default}", tip], main)
    if not base:
        return False
    git = context.observer(main)
    paths = obs.changed_paths(git, base.strip(), tip)
    branch_entries = obs.tree_entries(git, tip)
    main_entries = context.main_entries(main, default, git)
    return all(branch_entries.get(path) == main_entries.get(path) for path in paths)


def _journal_landed(
    main: Path,
    journal: dict[str, Any],
    branch: str,
    tip: str,
    tree: str | None,
    context: ResolutionContext,
) -> bool:
    if journal.get("state") != "done":
        return False
    candidate = str(journal.get("cand") or "")
    if not candidate or not context.ancestor(main, candidate, "main"):
        return False
    members = journal.get("members") or [{"branch": journal.get("branch"), "tip": journal.get("tip")}]
    for raw in members:
        member = as_dict(raw)
        if str(member.get("branch") or "").removeprefix("refs/heads/") == branch:
            if member.get("tip") == tip:
                return True
            if tree and tree == context.tree(main, candidate):
                return True
    return False


def identity(main: Path, path: Path, branch: str) -> dict[str, Any]:
    """What a removal decision is bound to: the path's two-way registration, its HEAD and the
    branch tip. A removal proceeds only while all three are unchanged."""
    # One read for both while the branch exists: a worktree sees its main checkout's refs.
    both = (
        repos.git_or_none(["rev-parse", "HEAD^{commit}", f"refs/heads/{branch}^{{commit}}"], path)
        if branch
        else None
    )
    if both and len(both.split()) == 2:
        head, tip = both.split()
    else:
        head = (repos.git_or_none(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], path) or "").strip()
        tip = ""
    return {"registration": worktree.registration(main, path), "head": head, "tip": tip}


def missing_hold(state: str) -> str:
    """A tree whose path is not present is reported, never removed or counted as removed."""
    how = (
        "preview `dispatch` action `reconcile` for a dispatch path; for a manager path, preview "
        "`harness` action `worktree_reconcile`"
    )
    return f"path {state}: not removed; {how}"


def removal_holds(
    root: Path,
    main: Path,
    path: Path,
    branch: str,
    feature: int | None,
    records: list[dict[str, Any]],
    *,
    default: str = "main",
    collected: dict[str, object] | None = None,
    force: bool = False,
    expect: dict[str, Any] | None = None,
    cwds: list[tuple[int, str]] | None = None,
    ignore_holder: str | None = None,
    session_id: str | None = None,
    context: ResolutionContext | None = None,
    require_resolved: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """The one removal policy for tidy, manager and dispatch cleanup, run when deciding and again
    under the coordination guards just before anything is unlinked. `records` are the dispatches
    on this path, re-read here. `collected` is the collected-file digests whose copies make an
    untracked file safe to lose (a dispatch's, `{}` for a manager checkout; None for tidy, whose
    untracked files all count as changes). `force` waives owner waits, real changes and an
    unresolved tip, never activity, unconfirmed cleanup or uncollected files. A manager removal
    keeps its branch, so it passes `require_resolved=False`. Returns the holds and the identity
    they were checked against; with `expect`, a changed identity holds."""
    seen = identity(main, path, branch)
    reg = seen["registration"]
    if reg["state"] != "present":
        return [missing_hold(reg["state"])], seen
    reasons = active_holds(root, path, feature, cwds=cwds, ignore_holder=ignore_holder, session_id=session_id)
    reasons.extend(dispatch_holds(root, records, force=force))
    if reg.get("locked"):
        reasons.append("its registration is locked (`git worktree lock`)")
    if not seen["head"] or seen["head"] != seen["tip"]:
        reasons.append(
            f"worktree HEAD {seen['head'] or '?'} is not the exact tip of {branch or '(no branch)'}; kept"
        )
    if expect is not None and seen != expect:
        reasons.append("its checkout, HEAD or branch changed while cleanup checked it; run again")
    safe: set[str] = set()
    harness_root = main if worktree.is_main_checkout_of(root, main) else None
    if collected is not None:
        from .dispatch.worktrees import uncollected_files

        uncollected, safe = uncollected_files(path, collected)
        if harness_root is not None:
            uncollected = [n for n in uncollected if n not in harness_exempt(path, harness_root)]
        safe |= set(uncollected)  # held below whatever `force` says; not listed twice
        if uncollected:
            how = (
                "collect them with `dispatch` action `collect-files`, or have the owner confirm "
                "`discard-files` for an abandoned worker's scratch"
                if records
                else "commit, move or delete them yourself; removal never discards them"
            )
            reasons.append(f"uncollected files: {', '.join(uncollected)} ({how})")
    if not force:
        real = changes(path, safe, harness_root=harness_root)
        if real:
            waive = " (dirty; pass force)" if collected is not None else ""
            reasons.append("uncommitted changes: " + ", ".join(real) + waive)
        if require_resolved and not resolved(root, main, branch, default, context=context):
            reasons.append(UNRESOLVED_BRANCH)
    return reasons, seen


def dispatch_holds(root: Path, group: list[dict[str, Any]], *, force: bool = False) -> list[str]:
    """Re-read known dispatches so a resume or pending verifier cannot free their tree or ref."""
    from .dispatch import state
    from .dispatch.verification_gate import requires_lease

    reasons: list[str] = []
    current: list[dict[str, Any]] = []
    for original in group:
        d = state.load(root, original["id"]) if original.get("id") else original
        current.append(d)
        if d.get("state") is not None and (
            d["state"] not in state.TERMINAL_STATES or requires_lease(root, d)
        ):
            reasons.append(f"dispatch {d.get('id', 'unknown')} is active or cleanup is unconfirmed")
    latest = max(reversed(current), key=lambda d: str(d.get("created") or ""), default=None)
    if not force and latest and latest.get("state") in ("question", "blocked"):
        reasons.append("waiting on the owner")
    return reasons


def _feature(branch: str) -> int | None:
    match = re.match(r"agent/(\d{3})(?:[-/]|$)", branch)
    return int(match[1]) if match else None


def inventory(
    root: Path,
    feature: int | None = None,
    *,
    manager_only: bool = False,
    session_id: str | None = None,
    progress: Callable[[str], None] | None = None,
    context: ResolutionContext | None = None,
) -> dict[str, Any]:
    """Closed-feature leftovers and terminal dispatch trees, including open features."""
    from .dispatch.state import TERMINAL_STATES

    known = {f.id: f for f in features.load_all(root)}
    if feature is not None:
        known = {fid: f for fid, f in known.items() if fid == feature}
    if feature is not None and not known:
        return {"trees": [], "branches": []}
    closed = {fid for fid, f in known.items() if not f.is_open}
    context = context if context is not None else ResolutionContext(root)
    records = context.records
    main = worktree.main_checkout(root)
    checkouts: dict[Path, str] = {main: "main"}
    checkout_repos: dict[Path, set[str]] = {}
    if not manager_only and repos.registry_path(root).exists():
        for repo in repos.load_registry(root).repos:
            path = repo.abs_path(root)
            if (path / ".git").exists():
                checkouts[path] = repo.default_branch
                checkout_repos.setdefault(path.resolve(), set()).update((repo.name, repo.short))
    elif repos.registry_path(root).exists():
        for repo in repos.load_registry(root).repos:
            if repo.abs_path(root).resolve() == main.resolve():
                checkout_repos.setdefault(main.resolve(), set()).update((repo.name, repo.short))
    trees: list[dict[str, Any]] = []
    branches: list[dict[str, Any]] = []
    inventory_error = ""
    try:
        cwds = process_cwds()
    except (core.AgentError, OSError, subprocess.SubprocessError) as exc:
        cwds = []
        inventory_error = f"process cwd inspection failed: {exc}"
    for checkout, default in checkouts.items():
        if progress:
            progress(f"inspecting {checkout}")
        context.refresh(checkout, default)
        location = {"main": str(checkout), "default_branch": default}
        entries = repos.worktrees(checkout, strict=True)
        checked = {entry.get("branch") for entry in entries}
        for entry in entries:
            path, branch = Path(entry["path"]), str(entry.get("branch") or "")
            if path.resolve() == checkout.resolve():
                continue
            group = [
                d for d in records if d.get("worktree") and Path(d["worktree"]).resolve() == path.resolve()
            ]
            if manager_only and group:
                continue
            fid = group[-1].get("feature") if group else _feature(branch)
            terminal = bool(group) and all(d.get("state") in TERMINAL_STATES for d in group)
            if fid not in known or (fid not in closed and not terminal):
                continue
            seen: dict[str, Any] = {}
            try:
                reasons, seen = removal_holds(
                    root,
                    checkout,
                    path,
                    branch,
                    fid,
                    group,
                    default=default,
                    cwds=cwds,
                    session_id=session_id,
                    context=context,
                )
                if inventory_error:
                    reasons.append(inventory_error)
            except (core.AgentError, OSError, subprocess.SubprocessError) as exc:
                reasons = [f"inspection failed: {exc}"]
            row = {**location, "path": str(path), "branch": branch, "feature": fid, "holds": reasons}
            state = as_dict(seen.get("registration")).get("state")
            if state and state != "present":
                row["path_state"] = state
            trees.append({**row, "identity": seen})
            if progress:
                progress(f"{path}: {'; '.join(reasons) if reasons else 'safe to remove'}")
        output = repos.run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], checkout)
        for branch in output.splitlines():
            group = [
                d
                for d in records
                if d.get("branch") == branch
                and d.get("repo") in checkout_repos.get(checkout.resolve(), set())
            ]
            if manager_only and group:
                continue
            fid = group[-1].get("feature") if group else _feature(branch)
            unchecked_agent = feature is None and branch.startswith("agent/") and branch not in checked
            if any(d.get("feature") not in known for d in group) and not unchecked_agent:
                continue
            terminal = bool(group) and all(d.get("state") in TERMINAL_STATES for d in group)
            if unchecked_agent or (
                fid in known and (fid in closed or terminal or (not group and branch not in checked))
            ):
                is_resolved = resolved(root, checkout, branch, default, context=context)
                reasons = dispatch_holds(root, group)
                hint = (
                    COPY_HINT
                    if not is_resolved and context.possibly_copied(checkout, branch, default)
                    else ""
                )
                branches.append(
                    {
                        **location,
                        "branch": branch,
                        "feature": fid,
                        "resolved": is_resolved,
                        "checked_out": branch in checked,
                        "holds": reasons,
                        **({"hint": hint} if hint else {}),
                    }
                )
                if progress:
                    progress(
                        f"{branch}: {'resolved' if is_resolved else 'unresolved; keep'}"
                        + (f"; {hint}" if hint else "")
                    )
    return {"trees": trees, "branches": branches}


def run(
    root: Path,
    *,
    apply: bool = False,
    feature: int | None = None,
    delete_branches: bool = True,
    manager_only: bool = False,
    session_id: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from .dispatch import state as dispatch_state
    from .dispatch.allocation import alloc_guard, git_admin, git_admin_short

    context = ResolutionContext(root)
    listing = inventory(
        root, feature, manager_only=manager_only, session_id=session_id, progress=progress, context=context
    )
    removed: list[str] = []
    deleted: list[str] = []
    if apply:
        for row in listing["trees"]:
            if row["holds"]:
                continue
            main, default = Path(row["main"]), row["default_branch"]
            path = Path(row["path"])
            seen = [
                d
                for d in context.records
                if d.get("worktree") and Path(d["worktree"]).resolve() == path.resolve()
            ]
            short = git_admin_short(root, main)
            # Rechecked just before the unlink with the path's whole membership read again. The
            # allocation guard keeps a new or resumed dispatch off the path until it is gone (it
            # precedes git-admin in the lock order); the records guard is never held while git runs.
            with alloc_guard(root, short), git_admin(root, short):
                context.refresh(main, default)
                group = [
                    d
                    for d in dispatch_state.load_all(root)
                    if d.get("worktree") and Path(d["worktree"]).resolve() == path.resolve()
                ]
                try:
                    row["holds"], _ = removal_holds(
                        root,
                        main,
                        path,
                        row["branch"],
                        row["feature"],
                        group,
                        default=default,
                        expect=row.get("identity"),
                        session_id=session_id,
                        context=context,
                    )
                    if not row["holds"] and _revisions(seen) != _revisions(group):
                        row["holds"] = ["its dispatch record changed while cleanup checked it; run again"]
                    if row["holds"]:
                        continue
                    worktree.unlink_links(path)
                    repos.run_git(["worktree", "remove", "--force", str(path)], main)
                    removed.append(str(path))
                except (core.AgentError, OSError, subprocess.SubprocessError) as exc:
                    row["holds"] = [f"removal failed: {exc}"]
        if delete_branches:
            for row in listing["branches"]:
                main, default = Path(row["main"]), row["default_branch"]
                with git_admin(root, git_admin_short(root, main)):
                    context.refresh(main, default)
                    branch = row["branch"]
                    group = [d for d in context.records if d.get("branch") == branch]
                    tip = branch_tip(main, branch)
                    if tip and not branch_holds(root, main, branch, default, group, context=context):
                        delete_branch(main, branch, tip)
                        deleted.append(branch)
    for row in listing["trees"]:
        row.pop("identity", None)
    return {**listing, "dry_run": not apply, "removed": removed, "branches_deleted": deleted}


def _revisions(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {str(d.get("id")): d.get("record_revision") for d in records}


def branch_tip(main: Path, branch: str) -> str:
    return (
        repos.git_or_none(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], main) or ""
    ).strip()


def branch_holds(
    root: Path,
    main: Path,
    branch: str,
    default: str,
    records: list[dict[str, Any]],
    *,
    context: ResolutionContext | None = None,
) -> list[str]:
    """Why a branch ref stays: one rule for tidy and dispatch cleanup. Only a harness-owned branch
    (agent/ or a dispatch's) that no registration checks out, including a missing one, whose
    exact tip is resolved and that no active or owner-waiting dispatch needs is deleted."""
    reasons: list[str] = []
    if not branch.startswith(worktree.BRANCH_PREFIX) and not any(d.get("branch") == branch for d in records):
        reasons.append("not a harness-owned branch")
    for entry in repos.worktrees(main, strict=True):
        if entry.get("branch") == branch:
            reasons.append(f"checked out at {entry['path']}")
    reasons.extend(dispatch_holds(root, records))
    if not resolved(root, main, branch, default, context=context):
        reasons.append("unresolved tip; kept")
    return reasons


def delete_branch(main: Path, branch: str, tip: str) -> None:
    """Delete the branch only while it still points at the `tip` its holds were checked against."""
    repos.run_git(["update-ref", "-d", f"refs/heads/{branch}", tip], main)


def operation(root: Path, *, apply: bool = False, feature: int | None = None):
    from .ops._shared import Op

    data = run(
        root,
        apply=apply,
        feature=feature,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    return Op(data, format_result(data))


EXAMPLES = 10
TIDY_LISTS = ("trees", "branches", "removed", "branches_deleted")


def bounded(data: dict[str, Any], keys: tuple[str, ...], evidence: Path | None) -> dict[str, Any]:
    """`data` with each list in `keys` cut to EXAMPLES rows, `counts` of the full lists and the
    `evidence` file holding them. The caller writes that file before cutting."""
    counts = {key: len(data.get(key) or []) for key in keys}
    cut = {key: list(data.get(key) or [])[:EXAMPLES] for key in keys}
    return {**data, **cut, "counts": counts, **({"evidence": str(evidence)} if evidence else {})}


def evidence_file(root: Path, kind: str, data: dict[str, Any]) -> Path:
    """The complete result behind a bounded report, in the scratch area tool output spills to."""
    import json
    import uuid
    from datetime import datetime

    from . import scratch

    directory = scratch.ensure(root) / "harness"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = directory / f"{kind}-{stamp}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return path


def report(root: Path, kind: str, data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Bound a cleanup result for a tool: past EXAMPLES rows in any list, the full one goes to a file."""
    long = any(len(data.get(key) or []) > EXAMPLES for key in keys)
    return bounded(data, keys, evidence_file(root, kind, data) if long else None)


def _more(lines: list[str], total: int, evidence: object) -> None:
    if total > EXAMPLES:
        where = f" in {evidence}" if evidence else ""
        lines.append(f"... {total - EXAMPLES} more{where}")


def format_result(data: dict[str, Any]) -> str:
    lines = ["tidy dry-run" if data["dry_run"] else "tidy apply"]
    counts = {key: int(str(value)) for key, value in as_dict(data.get("counts")).items()}
    trees, branches = data["trees"], data["branches"]
    if counts:
        lines.append(
            f"trees: {counts.get('trees', len(trees))}; branches: {counts.get('branches', len(branches))}; "
            f"removed: {counts.get('removed', 0)}; branches deleted: {counts.get('branches_deleted', 0)}"
        )
    for row in trees[:EXAMPLES]:
        action = "; ".join(row["holds"]) if row["holds"] else "safe to remove"
        lines.append(f"{row['path']} ({row['branch']}): {action}")
    _more(lines, counts.get("trees", len(trees)), data.get("evidence"))
    for row in branches[:EXAMPLES]:
        hint = f"; {row['hint']}" if row.get("hint") else ""
        action = "unresolved; keep"
        if row.get("holds"):
            action = "; ".join(row["holds"])
        elif row["resolved"]:
            action = "resolved; checked out" if row.get("checked_out") else "resolved; safe to remove"
        lines.append(f"{row['branch']}: {action}{hint}")
    _more(lines, counts.get("branches", len(branches)), data.get("evidence"))
    return "\n".join(lines)
