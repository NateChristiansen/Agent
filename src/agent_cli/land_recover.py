"""Finish or undo an interrupted `agent land`, from what is observed rather than what was written.

Phase A observes with no write of any kind: the journal's process is dead (its pid is gone, or
carries another start time), `.git/index.lock` is absent or holds exactly this journal's marker,
main is `old` or `cand`, the index is `old` or `cand`, and every affected path is classified
`at-old`, `at-cand` or `other` without running any git filter. Any `other` path, a foreign lock,
or a main that names neither commit stops recovery with nothing changed. Phase B journals the plan
it chose, then acts, and releases the lock last:

- main `old`, index `old`, every path `at-old`: nothing was applied. Drop the temp index and the
  lock, then roll forward from step 2 when the record still applies, else mark it aborted.
- main `old`, index `old`, some paths `at-cand`, lock ours: the crash came during or after
  `read-tree` and before the compare-and-swap. The lock has been held since step 2, so no other
  process wrote the index. Put our writes back from the unchanged index, then as above.
- main `cand`, index `old`, every path `at-cand`, lock ours: the crash came after the swap of the
  ref and before the index rename. Rename the temp index into place, or rebuild it, then finish.
- main `cand`, index `cand`, every path `at-cand`: remove the lock if it is ours, then finish.
- anything else: stop and report.

`--abort` is the owner's way out of a stalled landing (main moved elsewhere mid-landing): with main
at any commit except the candidate, it puts our writes back when every path is `at-cand` or
`at-old` and the index is still `old`, then releases the lock.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import land_followups as followups
from . import land_journal as jn
from . import land_observe as obs
from .core import AgentError


def observe(main: Path, git: obs.Git, journal: dict[str, Any]) -> dict[str, Any]:
    """Phase A: everything recovery decides on, read without writing."""
    from .land import entries, object_format

    old_e, cand_e = entries(git, journal)
    ref = git.run("rev-parse", "--verify", "--quiet", "refs/heads/main", ok=(0, 1)).strip()
    try:
        index_now = obs.index_entries(git)
        index = "old" if index_now == old_e else "cand" if index_now == cand_e else "neither"
    except AgentError:
        index = "neither"
    config = git.settings()
    _, crlf = obs.unsupported(git, journal["paths"], old_e, cand_e, (journal["old"], journal["cand"]), config)
    classes = obs.classify(
        main, journal["paths"], old_e, cand_e, object_format(git), crlf=crlf, config=config
    )
    temp = jn.temp_index(main, journal["id"])
    try:
        temp_ok = temp.is_file() and obs.index_entries(git, temp) == cand_e
    except AgentError:
        temp_ok = False
    counts = {c: sum(1 for v in classes.values() if v == c) for c in (obs.AT_OLD, obs.AT_CAND, obs.OTHER)}
    return {
        "ref": ref,
        "main": "old" if ref == journal["old"] else "cand" if ref == journal["cand"] else "elsewhere",
        "index": index,
        "classes": classes,
        "lock": jn.lock_state(main, journal),
        "temp_ok": temp_ok,
        "summary": {"main": ref, "index": index, **counts},
    }


def undo(main: Path, git: obs.Git, journal: dict[str, Any]) -> None:
    """Put back the paths land wrote, from the real index, which still holds `old`. Paths the
    candidate added are removed; `checkout-index` without `-u` writes no index."""
    from .land import entries, object_format

    old_e, cand_e = entries(git, journal)
    config = git.settings()
    _, crlf = obs.unsupported(git, journal["paths"], old_e, cand_e, (journal["old"], journal["cand"]), config)
    classes = obs.classify(
        main, journal["paths"], old_e, cand_e, object_format(git), crlf=crlf, config=config
    )
    ours = sorted(p for p, c in classes.items() if c == obs.AT_CAND and old_e.get(p) != cand_e.get(p))
    for path in (p for p in ours if p not in old_e):
        full = main / path
        if full.is_symlink() or full.is_file():
            full.unlink()
        parent = full.parent
        while parent != main and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    present = [p for p in ours if p in old_e]
    if present:
        git.run("checkout-index", "-f", "--", *present)


def _stop(journal: dict[str, Any], why: str) -> AgentError:
    return AgentError(f"land {journal['id']}: {why}; nothing was changed")


def _validated(main: Path, git: obs.Git, journal: dict[str, Any]) -> dict[str, Any]:
    if jn.alive(journal):
        raise AgentError(f"land {journal['id']} is still running (pid {journal['pid']})")
    seen = observe(main, git, journal)
    if seen["lock"] == "foreign":
        raise _stop(journal, ".git/index.lock belongs to another process, and land never removes it")
    other = sorted(p for p, c in seen["classes"].items() if c == obs.OTHER)
    if other:
        raise _stop(journal, "these paths changed after the landing began: " + ", ".join(other[:20]))
    return seen


def recover(root: Path, ident: str) -> dict[str, Any]:
    from .land import main_of

    main = main_of(root)
    with jn.flock(main):
        return _recover(root, main, jn.read(main, ident))


def recover_all(root: Path, main: Path) -> None:
    """Before a new landing: every unfinished one recovers, or the new one is refused. A done
    landing from the last week that still owes its milestone or notices settles them too."""
    for journal in jn.pending(main):
        _recover(root, main, journal)
    for journal in _owing(main):
        followups.reconcile(root, main, journal)


OWING_DAYS = 7


def _owing(main: Path) -> list[dict[str, Any]]:
    """Done landings from the last `OWING_DAYS` with an obligation still pending."""
    from datetime import datetime, timedelta

    from .core import now

    cutoff = now() - timedelta(days=OWING_DAYS)
    out: list[dict[str, Any]] = []
    for journal in jn.every(main):
        if not followups.outstanding(journal):
            continue
        try:
            if datetime.fromisoformat(str(journal.get("ts"))) < cutoff:
                continue
        except TypeError, ValueError:
            continue
        out.append(journal)
    return out


def _recover(root: Path, main: Path, journal: dict[str, Any]) -> dict[str, Any]:
    if journal["state"] in jn.TERMINAL:
        # A done landing may still owe its milestone or notices: settle them, metadata only,
        # with no Git replay. Aborted and legacy journals owe nothing.
        if followups.outstanding(journal):
            followups.reconcile(root, main, journal)
        return journal
    git = obs.Git(main)
    seen = _validated(main, git, journal)
    if seen["main"] == "elsewhere":
        raise _stop(
            journal,
            f"main is at {seen['ref'][:12]}, neither the old main nor the candidate. The index lock stays "
            f"held. Run `agent land --abort {journal['id']}` to put your files and index back at the old "
            "main, or resolve it by hand",
        )
    at_cand = [p for p, c in seen["classes"].items() if c == obs.AT_CAND]
    every_cand = len(at_cand) == len(seen["classes"])
    temp = jn.temp_index(main, journal["id"])
    if seen["main"] == "old" and seen["index"] == "old":
        if at_cand and seen["lock"] != "ours":
            raise _stop(journal, "files moved without land's index lock, which land never allows")
        jn.advance(main, journal, journal["state"], recovery="undo, then roll forward or abort")
        if at_cand:
            undo(main, git, journal)
            if any(c != obs.AT_OLD for c in observe(main, git, journal)["classes"].values()):
                raise _stop(journal, "the files did not all return to the old main")
        temp.unlink(missing_ok=True)
        jn.release(main, journal)
        return _forward(root, main, git, journal)
    if seen["main"] == "cand" and seen["index"] == "old" and every_cand and seen["lock"] == "ours":
        jn.advance(main, journal, journal["state"], recovery="swap the index, then finish")
        if not seen["temp_ok"]:
            rebuilt = temp.with_suffix(".rebuild")
            git.with_index(rebuilt).run("read-tree", journal["cand"])
            os.replace(rebuilt, temp)
        os.replace(temp, jn.index_file(main))
        return _finish(main, git, journal)
    if seen["main"] == "cand" and seen["index"] == "cand" and every_cand:
        jn.advance(main, journal, journal["state"], recovery="finish")
        temp.unlink(missing_ok=True)
        return _finish(main, git, journal)
    raise _stop(journal, f"main is {seen['main']}, the index is {seen['index']}: no safe way forward")


def _finish(main: Path, git: obs.Git, journal: dict[str, Any]) -> dict[str, Any]:
    from .land import differs_error, verify_done

    differs = verify_done(main, git, journal)
    jn.release(main, journal)
    if differs:
        raise differs_error(journal, differs)
    done = jn.advance(main, journal, "done")
    # A recovered landing records its integration too, with the tip the journal saved before the
    # transaction (recovery skipped it, so a squash builder stayed held),
    # and settles what the landing owes.
    followups.reconcile(main, main, done)
    return done


def _forward(root: Path, main: Path, git: obs.Git, journal: dict[str, Any]) -> dict[str, Any]:
    from . import verify_record
    from .land import check_prepared, transact

    try:
        found, why = verify_record.lookup(root, journal["cand"])
        if found is not None:
            check_prepared(main, git, journal)
    except AgentError as exc:
        found, why = None, str(exc)
    if found is None:
        return jn.advance(main, journal, "aborted", why=f"not rolled forward: {why}")
    done = transact(main, git, journal)
    followups.reconcile(main, main, done)
    return done


def abort(root: Path, ident: str) -> dict[str, Any]:
    """The owner's undo for a stalled landing: put our writes back and release the lock."""
    from .land import main_of

    main = main_of(root)
    with jn.flock(main):
        journal = jn.read(main, ident)
        if journal["state"] in jn.TERMINAL:
            return journal
        git = obs.Git(main)
        seen = _validated(main, git, journal)
        if seen["main"] == "cand":
            raise _stop(
                journal,
                f"main already names the candidate; run `agent land --recover {journal['id']}` instead",
            )
        if seen["index"] != "old":
            raise _stop(journal, f"the index is {seen['index']}, not the old main")
        jn.advance(main, journal, journal["state"], recovery="abort: put the files back")
        undo(main, git, journal)
        if any(c != obs.AT_OLD for c in observe(main, git, journal)["classes"].values()):
            raise _stop(journal, "the files did not all return to the old main")
        jn.temp_index(main, ident).unlink(missing_ok=True)
        jn.release(main, journal)
        note = "" if seen["main"] == "old" else f"; main is at {seen['ref'][:12]}, moved outside agent land"
        return jn.advance(
            main, journal, "aborted", why=f"aborted by the owner; files and index are at the old main{note}"
        )


def report(root: Path, *, include_certificate: bool = True) -> list[str]:
    """What doctor and session start say about landing: unfinished landings, main moves that
    were not `agent land`, and a main tip with no applicable pass record."""
    from . import verify_record
    from .land import main_of

    try:
        main = main_of(root)
    except AgentError, OSError:
        return []
    if not jn.land_dir(main).is_dir():
        return []
    lines = [
        f"land {j['id']} stopped at {j['state']}; run `agent land --recover {j['id']}`"
        for j in jn.pending(main)
    ]
    lines += outside_moves(main)
    if not include_certificate:
        return lines
    try:
        certified = verify_record.lookup(root, "refs/heads/main")[0] is not None
    except AgentError:
        certified = False
    if not certified:
        lines.append("main's tip has no full pass record; run `agent verify record main` before pushing")
    return lines


def outside_moves(main: Path) -> list[str]:
    """Main's reflog since the last finished landing, minus the moves landings made."""
    done = {j["id"]: j for j in jn.every(main) if j.get("state") == "done"}
    if not done:
        return []
    git = obs.Git(main)
    rows = git.run("reflog", "show", "--format=%H%x00%gs", "refs/heads/main", ok=(0, 128)).splitlines()
    out: list[str] = []
    for row in rows:
        sha, _, message = row.partition("\0")
        if message.startswith("agent land ") and message.split()[2] in done:
            break
        if _synced(git, sha, message):
            continue
        out.append(f"main moved outside agent land: {sha[:12]} {message}")
    return out


def _synced(git: obs.Git, sha: str, message: str) -> bool:
    """A move `repos_sync` harness=true made: its reflog message,
    to a tip its private ref fetched from the pinned URL. That tip was landed and certified on
    the machine that pushed it, so it is not an outside move. `git pull` never counts."""
    from .main_sync import REFLOG, SYNC_REF

    if not message.startswith(REFLOG):
        return False
    return git.run("merge-base", sha, SYNC_REF, ok=(0, 1, 128)).strip() == sha
