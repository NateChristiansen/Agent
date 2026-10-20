# ruff: noqa
from __future__ import annotations
import subprocess
import sys
from pathlib import Path
from .. import repos as repos_mod
from ..core import AgentError, ExternalFailure


def ref_oid(main: Path, ref: str) -> str | None:
    out = repos_mod.git_or_none(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], main)
    return out.strip() if out else None


def is_ancestor(main: Path, older: str, newer: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=main,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise ExternalFailure(f"git merge-base --is-ancestor in {main}: {proc.stderr.strip()}")
    return proc.returncode == 0


def _is_oid(value: str) -> bool:
    return len(value) in (40, 64) and all((c in "0123456789abcdef" for c in value))


def remote_head(main: Path, branch: str) -> str | None:
    if "origin" not in repos_mod.run_git(["remote"], main).split():
        return None
    want = f"refs/heads/{branch}"
    rows = repos_mod.run_git(["ls-remote", "--heads", "origin", want], main).splitlines()
    heads = [oid for oid, _, ref in (row.partition("\t") for row in rows) if ref == want]
    if not heads:
        return None
    if len(heads) > 1 or not _is_oid(heads[0]):
        raise AgentError(f"origin returned an invalid head for {branch}")
    oid = heads[0]
    repos_mod.run_git(["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "origin", oid], main)
    if ref_oid(main, oid) != oid:
        raise AgentError(f"fetched {branch} does not match origin's head {oid}")
    return oid


def resume_branch(main: Path, branch: str, base: str) -> tuple[str, bool]:
    local = ref_oid(main, f"refs/heads/{branch}")
    if local is not None:
        return (local, True)
    if base.startswith("origin/"):
        try:
            repos_mod.run_git(["fetch", "--quiet", "origin", base.split("/", 1)[1]], main)
        except ExternalFailure as exc:
            sys.stderr.write(f"note: {exc.message}; using the local {base}\n")
    return (base, False)


def reusable_checkout(main: Path, path: Path, branch: str) -> Path | None:
    if (repos_mod.git_or_none(["status", "--porcelain"], path) or "").strip():
        raise AgentError(f"reused worktree {path} has uncommitted changes; preserving it")
    existing = repos_mod.worktrees(main)
    if not any((Path(w["path"]).resolve() == path.resolve() for w in existing)):
        raise AgentError(f"{path} is not a registered worktree of {main}")
    if any((w.get("branch") == branch and Path(w["path"]).resolve() != path.resolve() for w in existing)):
        raise AgentError(f"branch {branch} is already checked out in another worktree; pass --branch")
    current = (repos_mod.git_or_none(["symbolic-ref", "--quiet", "--short", "HEAD"], path) or "").strip()
    return path if current == branch else None


def fresh_branch(main: Path, branch: str, base: str, *, occupied: Path | None = None) -> tuple[str, bool]:
    repos_mod.run_git(["check-ref-format", "--branch", branch], main)
    local = ref_oid(main, f"refs/heads/{branch}")
    remote = remote_head(main, branch)
    if remote is not None:
        if local is not None and local != remote:
            if not is_ancestor(main, local, remote):
                raise AgentError(f"local branch {branch} is ahead of or diverges from origin; preserving it")
            if occupied is None:
                repos_mod.run_git(["update-ref", f"refs/heads/{branch}", remote, local], main)
            else:
                repos_mod.run_git(["merge", "--ff-only", "--quiet", remote], occupied)
        return (remote, local is not None)
    if local is not None:
        return (local, True)
    if base.startswith("origin/"):
        repos_mod.run_git(["fetch", "--quiet", "--no-tags", "origin", base.split("/", 1)[1]], main)
    start = ref_oid(main, base)
    if start is None:
        raise AgentError(f"base {base!r} is not a commit in {main}")
    return (start, False)
