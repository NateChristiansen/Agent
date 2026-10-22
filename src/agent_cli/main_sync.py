"""Fast-forward the harness's own main to the pushed tip.

A machine that lands through another (such as a Windows harness landing through a Mac)
never lands the commits it needs, so `agent land` cannot bring its main up to date, and the main
guard refuses every git spelling that moves main. `repos_sync` with harness=true is the one other
way main moves, and only through this module. Everything below runs under the land lock, taken
before the fetch:

1. **A pinned source.** The URL comes from `harness.sync_url` in this machine's
   `agent.local.yaml`, the URL the landing machine pushes main to. Tips pushed there passed
   ADR-0041's pre-push full-pass gate. `remote.origin.url` is never read: the hook-level
   allowance this replaces was beaten by a repointed origin, a tag named `origin/main` and a
   forced fetch refspec.
2. **A fresh private ref.** Main is snapshotted, any `refs/agent/sync/main` is deleted without
   dereferencing it (a symref there aliased main), and the fetch writes
   a ref name that did not exist, `refs/agent/sync/<uuid>`: `git fetch <url>
   +refs/heads/main:<that ref>` with `--refmap=` (no configured refspec applies), no tags, no
   FETCH_HEAD, no prune and no maintenance. The user's config stays, because credential helpers
   live there; a URL rewrite (`insteadOf`) that changes the pinned URL refuses the sync. If main,
   or any ref outside `refs/agent/sync/`, changed across the fetch, the sync refuses.
3. **Checks.** No unfinished landing; main an ancestor of the fetched sha; the fetched tree within
   local main's suite budget; main checked out in at most one place.
4. **A checked move.** In the checkout that has main: HEAD must be main and the tracked tree
   clean, then `merge --ff-only <sha>`. With no such checkout: a compare-and-swap `update-ref`,
   with the checkouts looked at again just before and just after it. Git has no lock that keeps
   `git switch main` or `git worktree add ... main` out, so a checkout that attached in between
   is detected: if its index is not the new tree, main is put back and the sync refuses.
   Success is reported only after `refs/heads/main` reads the new sha and every checkout holding
   main has the new tree in its index.

A rollback undoes only this sync's own write: main goes back to the snapshot by compare-and-swap
from the fetched sha, and only when main reads exactly that. Any other value of main, a
concurrent local commit say, is left as it is and reported.

The fetched sha is kept as a plain `refs/agent/sync/main`, and the reflog entry starts with
`REFLOG`; `land_recover.outside_moves` recognises both.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from . import land_journal as jn
from .core import AgentError
from .records.narrow import as_dict_or_none

MAIN = "refs/heads/main"
SYNC_REF = "refs/agent/sync/main"
SYNC_PREFIX = "refs/agent/sync/"
REFLOG = "agent sync-main"
URL_KEY = "sync_url"


def _git(
    at: Path, *args: str, ok: tuple[int, ...] = (0,), env: dict[str, str] | None = None
) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=at,
        env={**os.environ, **(env or {})},
        capture_output=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if proc.returncode not in ok:
        raise AgentError(f"git {args[0]} in {at} failed; main was not moved: {proc.stderr.strip()}")
    return proc.returncode, proc.stdout.strip()


def _rev(at: Path, ref: str) -> str:
    return _git(at, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")[1]


def pinned_url(root: Path) -> str:
    """`harness.sync_url` from this machine's `agent.local.yaml`, or a refusal saying how to set it."""
    from .config_file import LOCAL, machine_root, machine_section

    block = as_dict_or_none(machine_section(root, "harness")) or {}
    url = block.get(URL_KEY)
    if isinstance(url, str) and url.strip():
        return url.strip()
    raise AgentError(
        f"no pinned harness URL; main was not moved. Set `harness: {{{URL_KEY}: <url>}}` in "
        f"{machine_root(root) / LOCAL} to the URL the landing machine pushes main to (ADR-0041)."
    )


def _refs(at: Path) -> dict[str, str]:
    """Every ref and its object, `refs/agent/sync/` aside."""
    out = _git(at, "for-each-ref", "--format=%(refname) %(objectname)")[1]
    pairs = (line.split(" ", 1) for line in out.splitlines() if line)
    return {name: sha for name, sha in pairs if not name.startswith(SYNC_PREFIX)}


def _absent(main: Path, ref: str) -> bool:
    """No ref of this name, plain or symbolic."""
    symbolic = _git(main, "symbolic-ref", "-q", ref, ok=(0, 1, 128))[0] == 0
    plain = _git(main, "show-ref", "--verify", "--quiet", ref, ok=(0, 1))[0] == 0
    return not symbolic and not plain


def _drop(main: Path, ref: str) -> None:
    """Delete `ref` itself, never what a symbolic ref of that name points at."""
    _git(main, "update-ref", "--no-deref", "-d", ref, ok=(0, 1))
    if not _absent(main, ref):
        raise AgentError(f"{ref} could not be removed; remove it by hand. Main was not moved.")


def _restore(main: Path, old: str, ours: str | None, why: str) -> AgentError:
    """Undo only this sync's own write: main goes back to `old` by compare-and-swap from `ours`,
    the sha this sync fetched, and only when main reads exactly that. Any other value is someone
    else's change, a concurrent local commit say, and is left as it is (restoring from whatever main read rewound a local commit). Returns the refusal."""
    now = _rev(main, MAIN)
    if now == old:
        return AgentError(f"{why}; main is at {old[:12]}, unchanged")
    if ours is not None and now == ours:
        code, _ = _git(
            main, "update-ref", "--no-deref", "-m", f"{REFLOG}: restore", MAIN, old, ours, ok=(0, 1, 128)
        )
        if code == 0:
            return AgentError(
                f"{why}; main had moved to the fetched {ours[:12]} and was put back to {old[:12]}"
            )
    now = _rev(main, MAIN)
    return AgentError(
        f"{why}; main changed during the sync to {now[:12]} and was left as is: check it by hand"
    )


def _fetched(main: Path, ref: str) -> str | None:
    code, sha = _git(
        main, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", ok=(0, 1, 128)
    )
    return sha if code == 0 and sha else None


def fetch(main: Path, url: str, old: str) -> str:
    """The pinned URL's main into a fresh private ref; returns its sha. The caller holds the land
    lock and passes main's sha from under it."""
    resolved = _git(main, "ls-remote", "--get-url", "--end-of-options", url)[1]
    if resolved != url:
        raise AgentError(
            f"git config rewrites the pinned URL to {resolved} (`url.*.insteadOf`); pin the URL "
            "itself or remove the rewrite. Main was not moved."
        )
    _drop(main, SYNC_REF)
    fresh = f"{SYNC_PREFIX}{uuid.uuid4().hex}"
    if not _absent(main, fresh):
        raise AgentError(f"{fresh} already exists; main was not moved")
    before = _refs(main)
    try:
        try:
            _git(
                main,
                *("-c", "fetch.prune=false", "-c", "fetch.pruneTags=false"),
                *("-c", "fetch.recurseSubmodules=false"),
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-auto-maintenance",
                "--refmap=",
                "--end-of-options",
                url,
                f"+{MAIN}:{fresh}",
            )
        except AgentError as exc:
            if _rev(main, MAIN) != old:
                why = f"the fetch failed and main moved: {exc.message}"
                raise _restore(main, old, _fetched(main, fresh), why) from exc
            raise
        after = _refs(main)
        changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
        if _rev(main, MAIN) != old or changed:
            why = f"refs outside {SYNC_PREFIX} changed during the fetch: {', '.join(changed)}"
            raise _restore(main, old, _fetched(main, fresh), why)
        new = _rev(main, fresh)
        _git(main, "update-ref", "--no-deref", SYNC_REF, new)
        return new
    finally:
        _git(main, "update-ref", "--no-deref", "-d", fresh, ok=(0, 1))


def _holders(main: Path) -> list[Path]:
    """Every checkout (main's or a linked worktree) with main checked out. `worktree add
    --force` can make more than one."""
    out = _git(main, "worktree", "list", "--porcelain")[1]
    found: list[Path] = []
    path: str | None = None
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            path = value
        elif key == "branch" and value == MAIN and path is not None:
            found.append(Path(path))
    return found


def _listed(paths: list[Path]) -> str:
    return ", ".join(str(p) for p in paths)


def _head(at: Path) -> str:
    return _git(at, "symbolic-ref", "-q", "HEAD", ok=(0, 1))[1]


def _index_is(at: Path, commit: str) -> bool:
    """The checkout's index holds exactly `commit`'s tree."""
    return _git(at, "diff-index", "--cached", "--quiet", commit, "--", ok=(0, 1))[0] == 0


def fast_forward(root: Path, budget_raise: str | None = None) -> dict[str, Any]:
    """Fetch the pinned URL's main and fast-forward local main to it; refuse when main has
    commits it lacks. `budget_raise` is the reason of the owner's `[[raise]]` entry the tip adds to
    the suite budget. Returns the old and new tips and what was done."""
    from .worktree import main_checkout

    main = main_checkout(root)
    url = pinned_url(root)
    with jn.flock(main):
        if unfinished := jn.pending(main):
            raise AgentError(
                f"a landing is unfinished; run `agent land --recover {unfinished[0]['id']}` first. "
                "Main was not moved."
            )
        old = _rev(main, MAIN)
        new = fetch(main, url, old)
        out: dict[str, Any] = {"old": old, "new": new, "url": url, "checkout": str(main)}
        if old == new:
            return {**out, "action": "already current"}
        if _git(main, "merge-base", "--is-ancestor", old, new, ok=(0, 1))[0]:
            raise AgentError(
                f"main ({old[:12]}) has commits the pushed main ({new[:12]}) lacks, so it cannot "
                "fast-forward; main was not moved. Push them from the machine that lands, or land them here."
            )
        behind = _git(main, "rev-list", "--count", f"{old}..{new}")[1]
        _within_budget(root, main, old, new, budget_raise)
        holders = _holders(main)
        if len(holders) > 1:
            raise AgentError(
                f"main is checked out in {len(holders)} places ({_listed(holders)}); a sync would "
                "leave all but one with old files. Remove the extra checkouts; main was not moved."
            )
        holder = holders[0] if holders else None
        if holder is None:
            _swap(main, old, new)
        else:
            _merge(holder, new)
        _verify(main, holder, new)
        return {**out, "action": f"fast-forwarded {behind} commit(s)"}


def _budget_text(main: Path, rev: str) -> str | None:
    from .suite_budget import PATH

    code, kind = _git(main, "cat-file", "-t", f"{rev}:{PATH}", ok=(0, 1, 128))
    if code:
        return None
    if kind != "blob":
        raise AgentError(f"{PATH} at {rev[:12]} is not a file; main was not moved")
    return _git(main, "cat-file", "blob", f"{rev}:{PATH}")[1]


def _within_budget(root: Path, main: Path, old: str, new: str, budget_raise: str | None) -> None:
    """The pushed tip keeps within local main's suite budget, by land's own check
    (suite_budget.py), held to its full pass record when this machine has one. Records are per
    machine and a tip landed elsewhere usually has none here, so without one only the budget files
    are compared."""
    from . import suite_budget, verify_record

    main_text = _budget_text(main, old)
    if main_text is None:
        return
    try:
        record = verify_record.lookup(root, new)[0]
    except AgentError:
        record = None
    if problems := suite_budget.check(record, main_text, _budget_text(main, new), budget_raise):
        raise AgentError(
            f"the pushed main {new[:12]} breaks the suite budget: {'; '.join(problems)}. Main was not "
            f"moved. Only the owner raises the budget: once they approve the [[raise]] entry the tip "
            f'adds to {suite_budget.PATH}, rerun the sync with budget_raise="<its reason>"'
        )


def _swap(main: Path, old: str, new: str) -> None:
    """Compare-and-swap main with no checkout holding it, looking again just before and just
    after. A checkout that attached in between with the old tree puts main back and refuses."""
    if attached := _holders(main):
        raise AgentError(
            f"{_listed(attached)} checked out main during the sync; main was not moved. Run it again."
        )
    _git(main, "update-ref", "--no-deref", "-m", f"{REFLOG}: fast-forward", MAIN, new, old)
    stale = [h for h in _holders(main) if not _index_is(h, new)]
    if stale:
        why = (
            f"{_listed(stale)} checked out main during the sync, with files at {old[:12]}; run the sync again"
        )
        raise _restore(main, old, new, why)


def _merge(holder: Path, new: str) -> None:
    """`merge --ff-only <sha>` in the checkout that has main, once HEAD is main there and its
    tracked tree is clean. Git itself refuses to overwrite an untracked file."""
    if _head(holder) != MAIN:
        raise AgentError(f"{holder} no longer has main checked out; main was not moved")
    if _git(holder, "status", "--porcelain", "--untracked-files=no")[1]:
        raise AgentError(f"{holder} has uncommitted changes to tracked files; main was not moved")
    _git(holder, "merge", "--ff-only", "--quiet", new, env={"GIT_REFLOG_ACTION": REFLOG})


def _verify(main: Path, holder: Path | None, new: str) -> None:
    """Main reads `new`; the checkout the sync merged in, if any, still has HEAD main; and every
    checkout holding main now has the new tree in its index. Otherwise say what is wrong; never
    report success."""
    now = _rev(main, MAIN)
    head = _head(holder) if holder is not None else MAIN
    if head != MAIN:
        raise AgentError(
            f"the sync did not leave main at {new[:12]}: main is at {now[:12]}; {holder} now has "
            f"{head or 'a detached HEAD'} checked out, check that branch"
        )
    if now != new:
        raise AgentError(f"the sync did not leave main at {new[:12]}: main is at {now[:12]}")
    stale = [h for h in _holders(main) if not _index_is(h, new)]
    if stale:
        raise AgentError(
            f"main is at {new[:12]}, but the index and files of {_listed(stale)} are not; check "
            "them before working there"
        )
