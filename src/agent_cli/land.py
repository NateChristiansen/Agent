"""`agent land`: move local main to a candidate with a full pass record, as one journaled
transaction (see ADR-0041).

The candidate is built against the main it replaces: the branch tip's tree and parents for
`ff`, or a merge or squash commit made with `git merge-tree` and `commit-tree` and kept on
`refs/agent/land/<id>`. Its message records member and origin tips as portable trailers.
Its tree gets a pass record, reused or run, before anything in the main checkout changes. Then,
under the land flock:

1. prepared: the configuration is supported, main is still `old`, the index is exactly `old`,
   and every path the landing changes is still at `old` on disk. Local edits to other files stay.
2. index_locked: land creates `.git/index.lock` with its marker. From here until step 5 releases
   it, no other git process can write the index or commit.
3. files_applying, files_applied: `read-tree -m -u old cand` on a copy of the index writes files.
4. ref_moving, ref_moved: `update-ref refs/heads/main cand old`, a compare-and-swap made while the
   lock is held. If main moved, the landing stalls: nothing is rolled back, the lock and the temp
   index stay, and the owner resolves it with `agent land --abort <id>` or by hand.
5. index_swapped: the temp index is renamed over `.git/index`; then the lock is removed.
6. done: the index, the files and main all name the candidate.

Success is never reported while the ref, the index and the files disagree. `land_recover`
finishes or undoes an interrupted landing from what it observes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import land_followups as followups
from . import land_journal as jn
from . import land_observe as obs
from .core import AgentError

BATCH_MAX = 4
STRATEGIES = ("squash", "merge", "ff")
REF_PREFIX = "refs/agent/land/"
SEAM = "AGENT_LAND_SEAM"


def main_of(root: Path) -> Path:
    from .worktree import main_checkout

    return main_checkout(root)


def _code(git: obs.Git, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [*git.args, *args], cwd=git.checkout, env=git.env, capture_output=True, encoding="utf-8", check=False
    )
    return proc.returncode, proc.stdout


def _commit(git: obs.Git, rev: str) -> str:
    code, out = _code(git, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")
    if code or not out.strip() or rev.startswith("-"):
        raise AgentError(f"{rev} is not a commit")
    return out.strip()


def main_ref(git: obs.Git) -> str:
    return _commit(git, "refs/heads/main")


def object_format(git: obs.Git) -> str:
    if not git.format:
        git.format = git.run("rev-parse", "--show-object-format").strip() or "sha1"
    return git.format


def _squash_message(git: obs.Git, old: str, tip: str, branch: str) -> str:
    subjects = git.run("log", "--reverse", "--format=%s", f"{old}..{tip}").splitlines()
    if len(subjects) == 1:
        return git.run("log", "-1", "--format=%B", tip).strip()
    return f"{branch}: {len(subjects)} commits\n\n" + "\n".join(f"- {s}" for s in subjects)


def default_strategy(git: obs.Git, old: str, tip: str) -> str:
    """`ff` for one commit on top of main, else `squash`."""
    descends = _code(git, "merge-base", "--is-ancestor", old, tip)[0] == 0
    count = int(git.run("rev-list", "--count", f"{old}..{tip}").strip() or "0")
    return "ff" if descends and count == 1 else "squash"


def candidate(
    git: obs.Git,
    old: str,
    tip: str,
    strategy: str,
    ident: str,
    branch: str,
    message: str | None,
    merge_base: str | None = None,
) -> str:
    """The commit main will move to, built against `old` and never in the main checkout."""
    if strategy not in STRATEGIES:
        raise AgentError(f"strategy must be one of {', '.join(STRATEGIES)}")
    if tip == old or _code(git, "merge-base", "--is-ancestor", tip, old)[0] == 0:
        raise AgentError(f"nothing to land: main already contains {branch}")
    if strategy == "ff":
        if _code(git, "merge-base", "--is-ancestor", old, tip)[0] != 0:
            raise AgentError(f"{branch} does not descend from main; land it with --strategy squash or merge")
        return tip
    if git.run("config", "--name-only", "--get-regexp", r"^merge\..*\.driver$", ok=(0, 1)).strip():
        raise AgentError("a custom merge driver is configured; land never runs one")
    base = [f"--merge-base={merge_base}"] if merge_base else []
    code, out = _code(git, "merge-tree", "--write-tree", "--no-messages", *base, old, tip)
    if code:
        raise AgentError(f"{branch} does not merge cleanly into main; rebase it first")
    tree = out.splitlines()[0].strip()
    if tree == git.run("rev-parse", f"{old}^{{tree}}").strip():
        # A squash-landed branch is never an ancestor of main, so the check above misses it; a
        # second landing made an empty commit in the phase-5 dogfood.
        raise AgentError(f"nothing to land: main already has every change on {branch}")
    parents = ["-p", old] + (["-p", tip] if strategy == "merge" else [])
    text = message or (f"Merge {branch}" if strategy == "merge" else _squash_message(git, old, tip, branch))
    cand = git.run("commit-tree", tree, *parents, "-m", text).strip()
    git.run("update-ref", REF_PREFIX + ident, cand)
    return cand


def refresh_adr_index(git: obs.Git, cand: str, ident: str) -> str:
    """Publish the derived index from the candidate's ADRs before certifying its tree."""
    from . import status

    entries = obs.tree_entries(git, cand)
    adr_paths = [p for p in entries if p.startswith("adrs/") and p.endswith(".md")]
    if not adr_paths:
        return cand
    with tempfile.TemporaryDirectory(prefix="agent-land-adrs-") as directory:
        root = Path(directory)
        for path in adr_paths:
            entry = entries[path]
            if entry.mode not in ("100644", "100755"):
                raise AgentError(f"{path}: the ADR index requires regular Markdown files")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git.blob(entry.oid))
        view = status.check(root)["views"].get(status.ADR_INDEX.as_posix())
        if view and view["fresh"]:
            return cand
        data = status.collect(root)
        text = status.render_adr_index(data, status.adr_hash(data))
        index = git.with_index(root / "index")
        index.run("read-tree", cand)
        rendered = root / "adr-index"
        rendered.write_bytes(text.encode("utf-8"))
        blob = index.run("hash-object", "-w", "--no-filters", "--", str(rendered)).strip()
        index.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{status.ADR_INDEX.as_posix()}")
        tree = index.run("write-tree").strip()
        parents = git.run("rev-list", "--parents", "-n", "1", cand).split()[1:]
        args = [arg for parent in parents for arg in ("-p", parent)]
        message = git.run("cat-file", "commit", cand).split("\n\n", 1)[1]
        name, email, date = git.run("show", "-s", "--format=%an%x00%ae%x00%aI", cand).strip().split("\0")
        author = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date}
        refreshed = git.run("commit-tree", tree, *args, stdin=message, env=author).strip()
        git.run("update-ref", REF_PREFIX + ident, refreshed)
        return refreshed


def checked_origins(git: obs.Git, origins: list[str]) -> list[tuple[str, str]]:
    """Resolve explicit source tips locally; a patch match cannot establish provenance."""
    checked: list[tuple[str, str]] = []
    for origin in origins:
        parts = origin.split()
        if len(parts) != 2:
            raise AgentError("each origin must be '<branch> <tip-sha>'")
        branch, tip = parts
        branch = branch.removeprefix("refs/heads/")
        if _code(git, "check-ref-format", f"refs/heads/{branch}")[0] or not re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", tip
        ):
            raise AgentError("each origin must name a branch and a full commit sha")
        try:
            commit = _commit(git, tip)
        except AgentError:
            raise AgentError(
                f"origin {branch} is not available locally; fetch the peer before landing"
            ) from None
        checked.append((branch, commit))
    return checked


def landed_tips(
    git: obs.Git,
    cand: str,
    ident: str,
    members: list[tuple[str, str]],
    origins: list[tuple[str, str]],
) -> str:
    """Put portable tip evidence on the candidate, preserving its tree and parents."""
    trailers = [f"Landed-tip: {branch.removeprefix('refs/heads/')} {tip}" for branch, tip in members]
    trailers += [f"Landed-tip: {branch} {tip} origin" for branch, tip in origins]
    message = git.run("cat-file", "commit", cand).split("\n\n", 1)[1].rstrip()
    message += "\n\n" + "\n".join(trailers) + "\n"
    parents = git.run("rev-list", "--parents", "-n", "1", cand).split()[1:]
    args = [arg for parent in parents for arg in ("-p", parent)]
    name, email, date = git.run("show", "-s", "--format=%an%x00%ae%x00%aI", cand).strip().split("\0")
    author = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date}
    result = git.run("commit-tree", f"{cand}^{{tree}}", *args, stdin=message, env=author).strip()
    git.run("update-ref", REF_PREFIX + ident, result)
    return result


def _seam(main: Path, journal: dict[str, Any], point: str) -> None:
    """A test seam: `AGENT_LAND_SEAM=<point>:exit` dies here as a crash would, and `:pause` waits
    for `<id>.resume` beside the journal (60 s at most)."""
    want = os.environ.get(SEAM, "")
    if not want.startswith(point + ":"):
        return
    if want.endswith(":exit"):
        os._exit(97)
    flag, resume = (
        jn.land_dir(main) / f"{journal['id']}.paused",
        jn.land_dir(main) / f"{journal['id']}.resume",
    )
    flag.write_text(point)
    deadline = time.monotonic() + 60
    while not resume.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    flag.unlink(missing_ok=True)
    resume.unlink(missing_ok=True)


def entries(git: obs.Git, journal: dict[str, Any]) -> tuple[dict[str, obs.Entry], dict[str, obs.Entry]]:
    return obs.tree_entries(git, journal["old"]), obs.tree_entries(git, journal["cand"])


def check_prepared(main: Path, git: obs.Git, journal: dict[str, Any]) -> None:
    """Step 1's conditions, with no write of any kind."""
    old_e, cand_e = entries(git, journal)
    config = git.settings()
    why, crlf = obs.unsupported(
        git, journal["paths"], old_e, cand_e, (journal["old"], journal["cand"]), config
    )
    hidden = obs.hidden_entries(git)
    if hidden:
        why.append("index entries marked skip-worktree or assume-unchanged: " + ", ".join(hidden[:10]))
    if why:
        raise AgentError("land cannot move main safely here: " + "; ".join(why))
    if git.run("symbolic-ref", "-q", "HEAD", ok=(0, 1)).strip() != "refs/heads/main":
        raise AgentError("the main checkout does not have main checked out")
    if main_ref(git) != journal["old"]:
        raise AgentError("main moved since the candidate was built; run `agent land` again")
    try:
        index = obs.index_entries(git)
    except AgentError as exc:
        raise AgentError(f"the main checkout's index is not clean: {exc}") from None
    if index != old_e:
        raise AgentError("the main checkout has staged changes; commit or unstage them first")
    seen = obs.classify(main, journal["paths"], old_e, cand_e, object_format(git), crlf=crlf, config=config)
    moved = sorted(p for p, c in seen.items() if c != obs.AT_OLD)
    if moved:
        raise AgentError("these files differ from main in the main checkout: " + ", ".join(moved[:20]))


def transact(main: Path, git: obs.Git, journal: dict[str, Any]) -> dict[str, Any]:
    """Steps 1 to 6 for a prepared journal. The caller holds the land flock."""
    check_prepared(main, git, journal)
    jn.advance(main, journal, "prepared")
    try:
        jn.take_lock(main, journal)
    except AgentError:
        jn.advance(main, journal, "aborted", why="another git process held the index")
        raise
    jn.advance(main, journal, "index_locked")
    try:
        # Again under the lock: a git process could have written the index or a file between the
        # first check and the lock, and none can from here until the lock is released.
        check_prepared(main, git, journal)
    except AgentError as exc:
        jn.release(main, journal)
        why = f"the checkout changed before the lock was taken; nothing was landed: {exc}"
        jn.advance(main, journal, "aborted", why=why)
        raise AgentError(why) from None
    _seam(main, journal, "index_locked")
    jn.advance(main, journal, "files_applying")
    temp = jn.temp_index(main, journal["id"])
    shutil.copyfile(jn.index_file(main), temp)
    staged = git.with_index(temp)
    # read-tree trusts cached stat data, so a file rewritten with its own bytes (by an editor, or
    # by recovery putting it back) reads as changed. Refresh the copy; the drivers stay blanked.
    _code(staged, "update-index", "-q", "--refresh")
    code, _ = _code(staged, "read-tree", "-m", "-u", journal["old"], journal["cand"])
    if code:
        from .land_recover import undo

        undo(main, git, journal)
        temp.unlink(missing_ok=True)
        jn.release(main, journal)
        jn.advance(main, journal, "aborted", why="read-tree failed; the files were put back")
        raise AgentError("git read-tree could not apply the candidate; nothing was landed")
    jn.advance(main, journal, "files_applied")
    _seam(main, journal, "files_applied")
    jn.advance(main, journal, "ref_moving")
    code, _ = _code(
        git,
        "update-ref",
        "-m",
        f"agent land {journal['id']}",
        "refs/heads/main",
        journal["cand"],
        journal["old"],
    )
    if code:
        from .land_recover import observe

        seen = observe(main, git, journal)
        jn.advance(main, journal, "stalled", main_at=seen["ref"], observed=seen["summary"])
        raise AgentError(
            f"main moved to {seen['ref'][:12]} during the landing, so it was not moved. Your files are at "
            f"the candidate and your index at the old main; the index lock is still held. Run "
            f"`agent land --abort {journal['id']}` to put the files back, or resolve it by hand."
        )
    jn.advance(main, journal, "ref_moved")
    _seam(main, journal, "ref_moved")
    os.replace(temp, jn.index_file(main))
    differs = verify_done(main, git, journal)
    jn.advance(main, journal, "index_swapped", **({"differs": differs} if differs else {}))
    _seam(main, journal, "index_renamed")
    jn.release(main, journal)
    _seam(main, journal, "released")
    if differs:
        raise differs_error(journal, differs)
    return jn.advance(main, journal, "done")


def differs_error(journal: dict[str, Any], differs: list[str]) -> AgentError:
    """Main and the index name the candidate, but these files do not: the landing is not done."""
    return AgentError(
        f"land {journal['id']}: main and the index are at {journal['cand'][:12]}, but these files differ "
        f"from it: {', '.join(differs[:20])}. Nothing was overwritten. Keep a copy of any edit you want, "
        f"restore each file from the candidate (`git checkout {journal['cand'][:12]} -- <path>`), then run "
        f"`agent land --recover {journal['id']}`."
    )


def verify_done(main: Path, git: obs.Git, journal: dict[str, Any]) -> list[str]:
    """Check, while the lock is still held, that main and the index name the candidate, and return
    the affected files that do not match it. A non-empty list means the landing is not done: the
    files are never overwritten, and the owner restores them before `--recover` finishes."""
    old_e, cand_e = entries(git, journal)
    problems: list[str] = []
    if main_ref(git) != journal["cand"]:
        problems.append("main does not name the candidate")
    if obs.index_entries(git) != cand_e:
        problems.append("the index is not the candidate's tree")
    if problems:
        raise AgentError(f"land {journal['id']} did not finish cleanly: " + "; ".join(problems))
    config = git.settings()
    _, crlf = obs.unsupported(git, journal["paths"], old_e, cand_e, (journal["old"], journal["cand"]), config)
    seen = obs.classify(main, journal["paths"], old_e, cand_e, object_format(git), crlf=crlf, config=config)
    return sorted(p for p, c in seen.items() if c != obs.AT_CAND)


def land(
    root: Path,
    branch: str,
    *,
    strategy: str | None = None,
    message: str | None = None,
    wait_s: float = 0.0,
    op: str | None = None,
    extra: list[str] | None = None,
    origins: list[str] | None = None,
    budget_raise: str | None = None,
) -> dict[str, Any]:
    """Land `branch` on local main. Returns the finished journal with the record it relied on.
    `op` names the landing operation whose stages `land_journal.op_update` records. `extra` lands
    a bounded batch (`batch_candidate`): one certificate for the exact combined tree, one journal.
    `origins` names pre-copy source tips, fetched locally and carried in the landed trailers.
    `budget_raise` is the reason of the owner's `[[raise]]` entry the candidate adds to the suite
    budget."""
    # Certificate locks support Windows as well as POSIX. Detached operation uses the shared
    # terminal flock.
    from .harness_role import consumer

    if consumer(root):
        raise AgentError(
            "this is a consumer install (harness.role: consumer); it never lands harness changes"
        )
    main = main_of(root)
    git = obs.Git(main)
    asked = time.monotonic()
    trail: dict[str, Any] = {"branch": branch, "members": 1 + len(extra or []), "op": op or ""}
    try:
        with jn.git_dir_scope():
            journal = _land(
                root,
                main,
                git,
                branch,
                strategy,
                message,
                wait_s,
                op,
                extra,
                asked,
                trail,
                origins,
                budget_raise,
            )
    except BaseException as exc:
        _op_event(main, trail, asked, "failed", why=str(exc))
        raise
    _op_event(main, trail, asked, "done")
    # After the landing is done and released: a held or failed cleanup stays on the journal.
    return followups.retry_cleanup(root, current=journal, main=main) or journal


def _op_event(main: Path, trail: dict[str, Any], asked: float, outcome: str, why: str = "") -> None:
    """One `land.op` telemetry event per landing request: what was asked, how long
    it waited for the land lock, which record kind certified it, and how it ended. Never raises."""
    from . import telemetry

    data = {k: v for k, v in trail.items() if k != "cand"}
    if why:
        data["why"] = why[:500]
    try:
        telemetry.record(
            main,
            "land.op",
            outcome=outcome,
            duration_s=round(time.monotonic() - asked, 3),
            commit=trail.get("cand"),
            data=data,
        )
    except Exception:  # noqa: BLE001 — telemetry never changes a landing's outcome
        return


def _certificate(
    root: Path, cand: str, *, git: obs.Git, old: str, budget_raise: str | None
) -> dict[str, Any]:
    """The full pass record for the candidate's tree (see ADR-0062): an existing one, or a new run.
    The record must also keep within main's suite budget (suite_budget.py)."""
    from . import suite_budget, verify_record

    record = verify_record.record(root, cand)
    problems = suite_budget.check(record, _budget_text(git, old), _budget_text(git, cand), budget_raise)
    if problems:
        raise AgentError(
            f"the suite budget refuses this landing: {'; '.join(problems)}. Only the owner raises the "
            f"budget: they add a [[raise]] entry to {suite_budget.PATH} and land with budget_raise "
            "set to its reason"
        )
    return record


def _budget_text(git: obs.Git, rev: str) -> str | None:
    from .suite_budget import PATH

    entry = obs.tree_entries(git, rev).get(PATH)
    if entry is None:
        return None
    if entry.mode not in ("100644", "100755"):
        raise AgentError(f"{PATH} at {rev[:12]} is not a regular file")
    return git.blob(entry.oid).decode("utf-8", "replace")


def record_kind(record: dict[str, Any]) -> str:
    """`retry-passed` (a full run whose only failures were approved timing flakes that passed their
    one serial retry) or `full`."""
    return "retry-passed" if record.get("retry") else "full"


def _land(
    root: Path,
    main: Path,
    git: obs.Git,
    branch: str,
    strategy: str | None,
    message: str | None,
    wait_s: float,
    op: str | None,
    extra: list[str] | None,
    asked: float,
    trail: dict[str, Any],
    origins: list[str] | None,
    budget_raise: str | None,
) -> dict[str, Any]:
    from . import land_recover, land_warning, verify_record
    from .tidy import ResolutionContext

    jn.op_update(main, op, state="waiting-for-lock", branch=branch, pid=os.getpid())
    with jn.flock(main, wait_s):
        trail["lock_wait_s"] = round(time.monotonic() - asked, 3)
        jn.op_update(main, op, state="verifying")
        land_recover.recover_all(root, main)
        ident = jn.new_id()
        old = main_ref(git)
        tip = _commit(git, branch)
        members = [(branch, tip), *((b, _commit(git, b)) for b in extra or [])]
        sources = checked_origins(git, origins or [])
        if tip in ResolutionContext(root, []).landed_tips(main, "main"):
            raise AgentError(f"nothing to land: main already contains {branch}")
        if len(members) > BATCH_MAX:
            raise AgentError(f"a batch lands at most {BATCH_MAX} branches")
        if len(members) > 1:
            chosen = strategy or "squash"
            cand = batch_candidate(git, old, members, chosen, ident)
        else:
            chosen = strategy or default_strategy(git, old, tip)
            cand = candidate(git, old, tip, chosen, ident, branch, message)
        cand = refresh_adr_index(git, cand, ident)
        check_prepared(main, git, {"old": old, "cand": cand, "paths": obs.changed_paths(git, old, cand)})
        foreign_lock = jn.lock_file(main).exists()
        # A known refusal must not create an evidence commit or ref for a fast-forward.
        if not foreign_lock:
            cand = landed_tips(git, cand, ident, members, sources)
        trail.update(strategy=chosen, cand=cand)
        try:
            record = _certificate(root, cand, git=git, old=old, budget_raise=budget_raise)
        except verify_record.RecordFailed as exc:
            trail["record_kind"] = "failed"
            if len(members) == 1:
                raise
            names = ", ".join(b for b, _ in members)
            raise AgentError(
                f"the batch {names} did not pass together, so nothing landed. The failing combination's "
                f"evidence is kept: {exc}. Land the members alone, or fix their interaction, then batch again."
            ) from None
        trail.update(record_kind=record_kind(record), record_reused=bool(record["reused"]))
        paths = obs.changed_paths(git, old, cand)
        try:
            restart = land_warning.notice(root, git, old, cand, paths)
        except Exception:  # noqa: BLE001 — the warning is advice; it never stops a landing
            restart = None
        journal = {
            "schema": 1,
            "id": ident,
            "pid": os.getpid(),
            "pid_start": jn.started(os.getpid()),
            "branch": branch,
            "tip": tip,
            "strategy": chosen,
            "old": old,
            "cand": cand,
            "paths": paths,
            "record": record["run_id"],
            "record_reused": record["reused"],
            "record_kind": record_kind(record),
            # Who must restart: wiring, MCP or state changes a live session holds.
            **({"restart": restart} if restart else {}),
            "state": "prepared",
            "ts": jn.now(),
            **({"members": [{"branch": b, "tip": t} for b, t in members]} if len(members) > 1 else {}),
            **({"origins": [{"branch": b, "tip": t} for b, t in sources]} if sources else {}),
            **({"budget_raise": budget_raise} if budget_raise else {}),
            # Record follow-up intent, worktree cleanup included, before marking done so recovery
            # can complete it after a crash.
            "obligations": followups.initial(),
            "session": os.environ.get("AGENT_LAND_SESSION") or None,  # who asked, for the notices
        }
        jn.op_update(main, op, state="landing", journal=ident, record=record["run_id"])
        if foreign_lock:
            jn.advance(main, journal, "aborted", why="another git process held the index")
            raise AgentError("another git process held the index")
        transact(main, git, journal)
        followups.reconcile(root, main, journal)  # integration marks, milestone, task notices
        return journal


def note_members(root: Path, journal: dict[str, Any]) -> None:
    """`note_integration` for every branch a journal landed: a batch's members, or its one branch."""
    members = journal.get("members") or [{"branch": journal.get("branch"), "tip": journal.get("tip")}]
    for member in members:
        note_integration(root, str(member.get("branch") or ""), journal, member.get("tip"))


def batch_candidate(git: obs.Git, old: str, members: list[tuple[str, str]], strategy: str, ident: str) -> str:
    """Build a bounded batch in member order so stacked branches land after their parents. Certify the
    exact combined tree once. If it fails, land nothing and name all members; do not infer a
    culprit.
    """
    if strategy not in ("squash", "merge"):
        raise AgentError("a batch lands with --strategy squash or merge")
    prev, applied = old, list[str]()
    for i, (branch, tip) in enumerate(members):
        # A member stacked on an earlier one is applied relative to the nearest such member: a
        # squash dropped that member's ancestry, so its edits read as conflicts .
        below = next(
            (t for t in reversed(applied) if _code(git, "merge-base", "--is-ancestor", t, tip)[0] == 0), None
        )
        prev = candidate(git, prev, tip, strategy, f"{ident}-{i}", branch, None, below)
        applied.append(tip)
    return prev


def note_integration(root: Path, branch: str, journal: dict[str, Any], tip: str | None = None) -> None:
    """Record on each dispatch whose branch just landed that its artifact is on main, with the
    journal and pass record. The attempt's own state is left as it was: a dispatch whose supervisor
    failed but whose branch landed stays failed, and its integration says landed. Best effort: the
    landing is already done, so a record that cannot be updated is skipped."""
    from . import verify_record
    from .dispatch import state as dispatch_state
    from .dispatch import telemetry_events
    from .dispatch.closing import mark_landed
    from .records.narrow import as_dict

    name = branch.removeprefix("refs/heads/")
    try:
        harness = verify_record.harness_repo(root).name
    except AgentError:
        return
    for d in dispatch_state.load_all(root):
        if d.get("branch") != name or d.get("repo") != harness:
            continue
        try:
            current = dispatch_state.load(root, str(d["id"]))
            previous = as_dict(current.get("integration"))
            if followups.newer_mark(root, previous, journal):
                # A later landing of this branch already wrote its integration: recovering an
                # older journal must not take it back.
                continue
            integration: dict[str, Any] = {
                "state": "landed",
                "journal": journal["id"],
                "record": journal["record"],
                "main": journal["cand"],
                "tip": tip,
                "ts": jn.now(),
            }
            # The task notices' delivery state belongs to this landing; a re-run for the same
            # landing keeps it, a later landing starts fresh.
            if previous.get("journal") == journal["id"] and previous.get("notices"):
                integration["notices"] = previous["notices"]
            current["integration"] = integration
            mark_landed(current, journal)
            dispatch_state.save(root, current)
            telemetry_events.transition(root, current, "landed", journal=journal["id"], main=journal["cand"])
        except AgentError, OSError:
            continue
