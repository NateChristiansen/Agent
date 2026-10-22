"""`agent land` (land.py, land_recover.py): main moves to a recorded candidate in one journaled
transaction, and an interrupted landing finishes or is undone from what recovery observes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cli import land, land_recover
from agent_cli import land_journal as jn
from agent_cli import land_observe as obs
from agent_cli.core import AgentError
from land_helpers import (  # noqa: F401
    certified,
    child,
    fingerprint,
    git,
    harness,
    only_journal,
    paused,
    recorded,
    resume,
)


def _at(root: Path, rev: str, *, except_: tuple[str, ...] = ()) -> bool:
    """Main, the index and the working files (all but `except_`) name `rev`."""
    g = obs.Git(root)
    entries = obs.tree_entries(g, rev)
    on_disk = all(obs.observed(root, p, "sha1") == e for p, e in entries.items() if p not in except_)
    return (
        git(root, "rev-parse", "refs/heads/main") == git(root, "rev-parse", rev)
        and obs.index_entries(g) == entries
        and on_disk
    )


@pytest.mark.parametrize("strategy", ["ff", "merge", "squash"])
def test_each_strategy_lands_and_main_index_and_files_agree(harness: Path, strategy: str):
    """Every strategy's candidate has the feature tree; ff certifies it with a real run."""
    if strategy != "ff":
        recorded(harness)
    journal = land.land(harness, "feature", strategy=strategy)
    assert journal["state"] == "done" and journal["strategy"] == strategy
    assert _at(harness, journal["cand"]) and not jn.lock_file(harness).exists()
    assert (harness / "a.txt").read_text() == "a2\n" and not (harness / "gone.txt").exists()
    parents = git(harness, "rev-list", "--parents", "-n1", journal["cand"]).split()[1:]
    assert len(parents) == {"ff": 1, "merge": 2, "squash": 1}[strategy]
    assert f"Landed-tip: feature {journal['tip']}" in git(harness, "show", "-s", "--format=%B", "main")
    if strategy == "ff":
        assert parents == git(harness, "show", "-s", "--format=%P", journal["tip"]).split()
    assert git(harness, "reflog", "-1", "--format=%gs", "refs/heads/main") == f"agent land {journal['id']}"


def test_the_default_is_ff_for_one_commit_and_squash_for_more(harness: Path):
    g = obs.Git(harness)
    old, tip = land.main_ref(g), land._commit(g, "feature")
    assert land.default_strategy(g, old, tip) == "ff"
    git(harness, "switch", "-q", "feature")
    git(harness, "commit", "-q", "--allow-empty", "-m", "second")
    git(harness, "switch", "-q", "main")
    assert land.default_strategy(g, old, land._commit(g, "feature")) == "squash"


def test_a_recorded_landing_keeps_an_unaffected_edit_and_reports_a_later_plumbing_move(harness: Path):
    """The candidate's record is reused, an unaffected local edit stays, and a later move of main
    outside land is reported."""
    recorded(harness)
    (harness / "b.txt").write_text("my edit\n")
    journal = land.land(harness, "feature", strategy="ff")
    assert journal["state"] == "done" and (harness / "b.txt").read_text() == "my edit\n"
    assert journal["record_reused"] is True
    sneaky = git(
        harness, "commit-tree", git(harness, "rev-parse", "main^{tree}"), "-p", "main", "-m", "sneaky"
    )
    git(harness, "update-ref", "-m", "python plumbing", "refs/heads/main", sneaky)
    lines = land_recover.report(harness)
    assert any("moved outside agent land" in line and "python plumbing" in line for line in lines)


@pytest.mark.parametrize("dirt", ["edit-affected", "stage-unaffected", "untracked-added"])
def test_a_dirty_main_checkout_is_refused_and_left_as_it_was(harness: Path, dirt: str):
    if dirt == "edit-affected":
        (harness / "a.txt").write_text("mine\n")
    elif dirt == "stage-unaffected":
        (harness / "b.txt").write_text("staged\n")
        git(harness, "add", "b.txt")
    else:
        (harness / "new.txt").write_text("untracked, in the way\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before and not jn.lock_file(harness).exists()


def test_main_moving_before_the_lock_refuses_and_a_second_try_lands(harness: Path, monkeypatch, certified):
    real = land._certificate

    def certify_then_move(root, cand, **kw):
        out = real(root, cand, **kw)
        (harness / "b.txt").write_text("b2\n")
        git(harness, "commit", "-qam", "someone else's commit")
        return out

    monkeypatch.setattr(land, "_certificate", certify_then_move)
    with pytest.raises(AgentError, match="main moved"):
        land.land(harness, "feature", strategy="squash")
    monkeypatch.setattr(land, "_certificate", real)
    assert land.land(harness, "feature", strategy="squash")["state"] == "done"
    assert (harness / "b.txt").read_text() == "b2\n" and (harness / "a.txt").read_text() == "a2\n"


def _git_write(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, capture_output=True, text=True
    )


@pytest.mark.parametrize("point", ["index_locked", "ref_moved", "index_renamed", "released"])
def test_a_crash_at_each_step_recovers_forward(harness: Path, point: str):
    """The landing is paused at `point`, checked live, then killed there. A live landing is never
    recovered from under it; from index_locked to index_renamed it is one index.lock held, so no
    commit can be made, and once released a git write goes through. After the kill it recovers
    forward; after the first step a new landing recovers the interrupted one first. The crash at
    files_applied, by the seam's own exit, is
    `test_recovery_after_one_crash_refuses_edits_and_foreign_locks_then_lands`."""
    recorded(harness)
    (harness / "b.txt").write_text("b-edit\n")
    proc = child(harness, f"{point}:pause")
    ident = paused(harness, proc)
    if point != "released":
        with pytest.raises(AgentError, match="holds the land lock"):
            land_recover.recover(harness, ident)
    if point in ("index_locked", "index_renamed"):
        for args in (("commit", "-am", "x"), ("add", "b.txt")):
            out = _git_write(harness, *args)
            assert out.returncode != 0 and "index.lock" in out.stderr, (point, args, out.stderr)
    elif point == "released":
        assert _git_write(harness, "add", "b.txt").returncode == 0
        assert _git_write(harness, "reset", "-q", "--", "b.txt").returncode == 0
    proc.kill()
    proc.wait(60)
    journal = only_journal(harness)
    if point == "index_locked":
        with pytest.raises(AgentError, match="nothing to land"):
            land.land(harness, "feature", strategy="ff")
        finished = only_journal(harness)
    else:
        finished = land_recover.recover(harness, journal["id"])
    assert finished["state"] == "done" and _at(harness, finished["cand"], except_=("b.txt",))
    assert not jn.lock_file(harness).exists() and not jn.temp_index(harness, journal["id"]).exists()
    assert (harness / "b.txt").read_text() == "b-edit\n"


def test_recovery_after_one_crash_refuses_edits_and_foreign_locks_then_lands(harness: Path):
    """One crash after the files were applied, then each refusal in turn (an owner's edit, a
    foreign lock, main moved), put back, and a forward recovery once the checkout is only partway
    through read-tree and the pid is reused."""
    recorded(harness)
    assert child(harness, "files_applied:exit").wait(60) == 97
    journal = only_journal(harness)
    # The owner edits the last path: recovery stops with zero changes.
    last = harness / sorted(journal["paths"])[-1]
    kept = last.read_bytes()
    last.write_text("the owner's own edit\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="changed after the landing began"):
        land_recover.recover(harness, journal["id"])
    assert fingerprint(harness) == before and jn.lock_state(harness, journal) == "ours"
    last.write_bytes(kept)
    # Someone else's lock in place of ours: refused, nothing changed.
    lock = jn.lock_file(harness)
    ours = lock.read_bytes()
    lock.write_text("replaced by someone else\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="another process"):
        land_recover.recover(harness, journal["id"])
    assert fingerprint(harness) == before
    lock.write_bytes(ours)
    # Main moved after the crash: recovery stops and keeps the lock.
    old = git(harness, "rev-parse", "main")
    elsewhere = git(harness, "commit-tree", git(harness, "rev-parse", "main^{tree}"), "-p", "main", "-m", "x")
    git(harness, "update-ref", "refs/heads/main", elsewhere)
    before = fingerprint(harness)
    with pytest.raises(AgentError):
        land_recover.recover(harness, journal["id"])
    assert fingerprint(harness) == before and jn.lock_state(harness, journal) == "ours"
    git(harness, "update-ref", "refs/heads/main", old)
    # A reused pid with another start time is dead.
    jn.release(harness, journal)
    journal.update(pid=__import__("os").getpid(), pid_start="Mon Jan  1 00:00:00 2001")
    jn.write(harness, journal)
    jn.take_lock(harness, journal)
    assert not jn.alive(journal)
    # read-tree died after writing some paths: one is at the candidate, the rest still old.
    (harness / "a.txt").write_bytes(b"a1\n")
    old_e, cand_e = land.entries(obs.Git(harness), journal)
    seen = obs.classify(harness, journal["paths"], old_e, cand_e, "sha1")
    assert sorted(set(seen.values())) == [obs.AT_CAND, obs.AT_OLD]
    finished = land_recover.recover(harness, journal["id"])
    assert finished["state"] == "done" and _at(harness, finished["cand"])
    assert not jn.lock_file(harness).exists() and not jn.temp_index(harness, journal["id"]).exists()


def test_a_foreign_index_lock_is_never_removed(harness: Path):
    recorded(harness)
    jn.lock_file(harness).write_text("some other git process\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="another git process"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before and jn.lock_file(harness).read_text() == "some other git process\n"
    assert only_journal(harness)["state"] == "aborted"


def test_main_moved_during_the_landing_stalls_with_no_rollback(harness: Path):
    recorded(harness)
    proc = child(harness, "files_applied:pause")
    ident = paused(harness, proc)
    elsewhere = git(
        harness, "commit-tree", git(harness, "rev-parse", "main^{tree}"), "-p", "main", "-m", "plumbing"
    )
    git(harness, "update-ref", "refs/heads/main", elsewhere)
    resume(harness, ident)
    assert proc.wait(60) == 3
    journal = jn.read(harness, ident)
    assert journal["state"] == "stalled" and journal["main_at"] == elsewhere
    assert jn.lock_state(harness, journal) == "ours" and jn.temp_index(harness, ident).exists()
    assert (harness / "a.txt").read_text() == "a2\n" and git(harness, "rev-parse", "main") == elsewhere
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="neither the old main"):
        land_recover.recover(harness, ident)
    assert fingerprint(harness) == before
    (harness / "a.txt").write_text("seeded edit\n")
    with pytest.raises(AgentError, match="changed after the landing began"):
        land_recover.abort(harness, ident)
    (harness / "a.txt").write_bytes(b"a2\n")
    done = land_recover.abort(harness, ident)
    assert done["state"] == "aborted" and elsewhere[:12] in done["why"]
    assert (harness / "a.txt").read_text() == "a1\n" and (harness / "gone.txt").exists()
    assert not (harness / "new.txt").exists() and not jn.lock_file(harness).exists()
    assert git(harness, "rev-parse", "main") == elsewhere


def test_a_git_write_before_the_lock_aborts_with_nothing_changed(harness: Path, monkeypatch):
    """Codex review: the index was checked before the lock and trusted after it. A `git add` in
    that gap left main at the candidate with an index that was neither."""
    recorded(harness)
    real = jn.take_lock

    def racing_take_lock(main: Path, journal: dict) -> None:
        (main / "other.txt").write_text("staged by another git process\n")
        git(main, "add", "other.txt")
        real(main, journal)

    monkeypatch.setattr(jn, "take_lock", racing_take_lock)
    old = git(harness, "rev-parse", "main")
    with pytest.raises(AgentError, match="changed before the lock"):
        land.land(harness, "feature", strategy="ff")
    assert git(harness, "rev-parse", "main") == old and (harness / "a.txt").read_text() == "a1\n"
    assert git(harness, "diff", "--cached", "--name-only") == "other.txt"
    assert not jn.lock_file(harness).exists() and only_journal(harness)["state"] == "aborted"


@pytest.mark.parametrize("strategy", ["ff", "merge", "batch"])
def test_landing_refreshes_stale_adr_index_before_certifying(harness: Path, strategy: str, monkeypatch):
    from agent_cli import status

    git(harness, "switch", "-q", "feature")
    adr = harness / "adrs/0001-example.md"
    adr.parent.mkdir()
    adr.write_text("---\nid: 1\ntitle: Original\nstatus: accepted\ndate: 2026-10-15\n---\n\n# Example\n")
    data = status.collect(harness)
    (harness / status.ADR_INDEX).write_text(
        status.render_adr_index(data, status.adr_hash(data)), encoding="utf-8"
    )
    git(harness, "add", "adrs")
    git(harness, "commit", "-qm", "ADR and index")
    adr.write_text(adr.read_text().replace("Original", "Revised"))
    git(harness, "commit", "-qam", "edit ADR without rendering")
    revised = status.collect(harness)
    tip = git(harness, "rev-parse", "feature")
    git(harness, "switch", "-q", "main")

    def certify(root, cand, **_):
        """What would be certified is the refreshed index; the verifier run itself is not under test."""
        assert "Revised" in git(harness, "show", f"{cand}:adrs/README.md")
        assert "Original" not in git(harness, "show", f"{cand}:adrs/README.md")
        published = obs.Git(harness).blob(git(harness, "rev-parse", f"{cand}:adrs/README.md"))
        assert published == status.render_adr_index(revised, status.adr_hash(revised)).encode("utf-8")
        return {"run_id": "certified-in-test", "reused": True}

    monkeypatch.setattr(land, "_certificate", certify)
    if strategy == "batch":
        git(harness, "switch", "-qc", "other", "main")
        (harness / "other.txt").write_text("other\n")
        git(harness, "add", "other.txt")
        git(harness, "commit", "-qm", "other")
        git(harness, "switch", "-q", "main")
    result = land.land(
        harness,
        "feature",
        strategy="squash" if strategy == "batch" else strategy,
        extra=["other"] if strategy == "batch" else None,
    )
    assert result["state"] == "done"
    assert (result["members"][0]["tip"] if strategy == "batch" else result["tip"]) == tip
    if strategy == "ff":
        # plan.md:86 requires trailers on main; author metadata and original parents still survive.
        metadata = "%an%x00%ae%x00%aI%x00%P"
        assert git(harness, "show", "-s", f"--format={metadata}", result["cand"]) == git(
            harness, "show", "-s", f"--format={metadata}", tip
        )
        assert git(harness, "show", "-s", "--format=%B", result["cand"]) == (
            git(harness, "show", "-s", "--format=%B", tip) + f"\n\nLanded-tip: feature {tip}"
        )
    assert status.check(harness)["views"]["adrs/README.md"]["fresh"] is True
    assert git(harness, "status", "--short", "--", "adrs") == ""


def test_adr_publication_refuses_linked_markdown_without_following_it(harness: Path):
    git(harness, "switch", "-q", "feature")
    (harness / "target.txt").write_text("external ADR source\n")
    git(harness, "add", "target.txt")
    blob = git(harness, "hash-object", "-w", "target.txt")
    git(harness, "update-index", "--add", "--cacheinfo", f"120000,{blob},adrs/0001-linked.md")
    git(harness, "commit", "-qm", "linked ADR")
    git(harness, "switch", "-q", "main")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="requires regular Markdown files"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before
