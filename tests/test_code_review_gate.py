"""ADR-0063: `land` and `push-guard` need a clean code-review round on every branch head; the
owner's override lets one through and is logged to the feature."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_cli import code_review, land, land_recover
from agent_cli import land_journal as jn
from agent_cli.core import AgentError
from hooks_test_helpers import _decision, _reason
from land_helpers import certified, git, harness, review_gate  # noqa: F401


def _branch(root: Path, name: str, start: str = "main", file: str = "b.txt") -> str:
    git(root, "switch", "-q", "-c", name, start)
    (root / file).write_text(f"{name}\n")
    git(root, "add", file)
    git(root, "commit", "-q", "-m", f"{name} work")
    git(root, "switch", "-q", "main")
    return git(root, "rev-parse", name)


def _round(root: Path, head: str, n: int = 1, *, branch: str, repo: str = "Harness", **over: Any) -> None:
    base = code_review.reviews_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    rec = {
        "repo": repo,
        "branch": branch,
        "head": head,
        "round": n,
        "phase": "complete",
        "blocking": [],
        "launched_at": f"2026-10-{10 + n}T00:00:00+00:00",
        **over,
    }
    (base / f"{head}-{n}.json").write_text(json.dumps(rec), encoding="utf-8")


def _dispatch(root: Path, dispatch_id: str, **fields: Any) -> None:
    ddir = root / ".agent" / "dispatches"
    ddir.mkdir(parents=True, exist_ok=True)
    record = {"id": dispatch_id, "feature": 1, "state": "done", **fields}
    (ddir / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


# ---------------------------------------------------------------- land


@pytest.fixture
def gated(review_gate: Path) -> Path:  # noqa: F811
    """The land harness with the real gate, and `fresh` one commit off main."""
    _branch(review_gate, "fresh")
    return review_gate


@pytest.fixture
def no_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal comes before the full suite: certifying fails the test."""

    def certify(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("the suite ran before the review gate refused")

    monkeypatch.setattr(land, "_certificate", certify)


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Feature notes, as (feature, text), for a fixture with no features."""
    from agent_cli import features, log

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(features, "resolve", lambda root, ref: f"feature {ref}")
    monkeypatch.setattr(log, "append", lambda f, kind, text: seen.append((f, text)))
    return seen


def test_a_clean_round_on_the_head_lands(gated: Path, certified):  # noqa: F811
    _round(gated, git(gated, "rev-parse", "fresh"), branch="fresh")
    assert land.land(gated, "fresh")["state"] == "done"


REFUSED = [
    ("missing", "no code-review round has run on it"),
    ("blocking", "kept verified blocking findings: F1"),
    ("running", "round 1 is still reviewing"),
    ("older", "its rounds are on older heads"),
    ("unreadable", "an unreadable or malformed round record"),
    ("empty object", "an unreadable or malformed round record"),
    ("no head", "an unreadable or malformed round record"),
    ("unknown phase", "round 1 is in an unknown phase 'paused'"),
]


@pytest.mark.parametrize(("case", "why"), REFUSED)
def test_a_head_with_no_clean_round_is_refused_before_the_suite(gated: Path, no_suite, case: str, why: str):
    tip = git(gated, "rev-parse", "fresh")
    beside = code_review.reviews_dir(gated) / f"{tip}-2.json"
    if case == "blocking":
        _round(gated, tip, branch="fresh", blocking=["F1"])
    elif case == "running":
        _round(gated, tip, branch="fresh", phase="reviewing", blocking=None)
    elif case == "older":
        _round(gated, git(gated, "rev-parse", "main"), branch="fresh")
    elif case == "unknown phase":
        _round(gated, tip, branch="fresh", phase="paused")
    elif case != "missing":
        _round(gated, tip, branch="fresh")
        broken = {"unreadable": "{not json", "empty object": "{}", "no head": '{"branch": "fresh"}'}
        beside.write_text(broken[case], encoding="utf-8")
    with pytest.raises(AgentError) as caught:
        land.land(gated, "fresh")
    text = str(caught.value)
    assert why in text and f"fresh at {tip[:12]}" in text and "`code-review`" in text
    base = git(gated, "rev-parse", "main")
    assert f'branch="fresh", base="{base}"' in text
    assert "`review_override=<reason>` if the owner accepts it unreviewed" in text


def test_a_branch_from_before_the_gate_is_refused_too(gated: Path, no_suite):
    """Every branch is gated: `feature` was cut before any of this, and still needs a round."""
    with pytest.raises(AgentError, match="feature at"):
        land.land(gated, "feature", strategy="squash")


def test_a_worker_branch_names_its_dispatch(gated: Path, no_suite):
    _dispatch(gated, "001-01", repo="Harness", branch="fresh", created="2026-10-01T00:00:00+00:00")
    with pytest.raises(AgentError, match='dispatch_id="001-01"'):
        land.land(gated, "fresh")


def test_a_batch_checks_every_member(gated: Path, no_suite):
    _round(gated, git(gated, "rev-parse", "fresh"), branch="fresh")
    _branch(gated, "other", file="c.txt")
    with pytest.raises(AgentError) as caught:
        land.land(gated, "fresh", extra=["other"])
    assert "other at" in str(caught.value) and "fresh at" not in str(caught.value)


def test_the_owner_override_lands_and_notes_the_feature(gated: Path, certified, notes):  # noqa: F811
    _dispatch(gated, "001-01", repo="Harness", branch="fresh")
    with pytest.raises(AgentError, match="needs the owner's reason"):
        land.land(gated, "fresh", review_override="  ")
    journal = land.land(gated, "fresh", review_override="hotfix, reviewed by hand")
    assert journal["state"] == "done" and journal["obligations"][land.REVIEW_NOTE] == "logged"
    held = journal["review_override"]
    assert held["reason"] == "hotfix, reviewed by hand" and held["members"][0]["branch"] == "fresh"
    [(where, text)] = notes
    assert where == "feature 1"
    assert "without a clean code-review round: no code-review round has run on it" in text
    assert "hotfix, reviewed by hand" in text
    land_recover.recover_all(gated, gated)  # settled: nothing is owed, and no second note
    assert len(notes) == 1


def test_an_override_with_no_feature_to_note_is_refused_and_says_how_to_bind(gated: Path, no_suite):
    with pytest.raises(AgentError, match="fresh has none") as caught:
        land.land(gated, "fresh", review_override="hotfix")
    assert "`worktree_new`" in str(caught.value) and "feature = the feature id" in str(caught.value)


def test_a_retried_override_note_writes_each_member_once(gated: Path, certified, notes, monkeypatch):  # noqa: F811
    from agent_cli import log

    _branch(gated, "other", file="c.txt")
    _dispatch(gated, "001-01", repo="Harness", branch="fresh")
    _dispatch(gated, "001-02", repo="Harness", branch="other")
    record = log.append
    fails = ["other"]

    def flaky(f: Any, kind: str, text: str) -> None:
        if fails and fails[0] in text:
            fails.pop()
            raise OSError("disk full")
        record(f, kind, text)

    monkeypatch.setattr(log, "append", flaky)
    journal = land.land(gated, "fresh", extra=["other"], review_override="hotfix")
    assert journal["obligations"][land.REVIEW_NOTE] == "pending"
    land_recover.recover_all(gated, gated)
    assert [text.split()[1] for _, text in notes] == ["fresh", "other"]
    assert jn.read(gated, journal["id"])["obligations"][land.REVIEW_NOTE] == "logged"


def test_an_override_landing_interrupted_before_done_notes_on_recovery(
    gated: Path,
    certified,  # noqa: F811
    notes,
    monkeypatch,
):
    """Dies where `AGENT_LAND_SEAM=released:exit` would, in process to spare the spawns."""
    _dispatch(gated, "001-01", repo="Harness", branch="fresh")
    seam = land._seam

    def crash(main: Path, journal: dict[str, Any], point: str) -> None:
        if point == "released":
            raise SystemExit(97)
        seam(main, journal, point)

    monkeypatch.setattr(land, "_seam", crash)
    with pytest.raises(SystemExit):
        land.land(gated, "fresh", strategy="ff", review_override="hotfix")
    monkeypatch.setattr(land, "_seam", seam)
    monkeypatch.setattr(jn, "alive", lambda journal: False)  # its process is this one, not a live landing
    assert not notes
    [journal] = jn.every(gated)
    assert journal["state"] != "done" and journal["obligations"][land.REVIEW_NOTE] == "pending"
    assert land_recover.recover(gated, journal["id"])["state"] == "done"
    [(_, text)] = notes
    assert "landed fresh" in text and "hotfix" in text
    assert jn.read(gated, journal["id"])["obligations"][land.REVIEW_NOTE] == "logged"


def test_the_mcp_land_action_hands_review_override_to_the_detached_cli(harness: Path, monkeypatch):  # noqa: F811
    from agent_cli.cli import parse
    from agent_cli.mcp.specs_harness import harness as harness_tool
    from agent_cli.mcp.tools import Ctx

    seen: list[str] = []
    popen = subprocess.Popen

    class Child:
        pid = 12345

    def spawn(argv, **kwargs):
        if "--op" not in argv:
            return popen(argv, **kwargs)
        seen.extend(argv)
        return Child()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    harness_tool(Ctx(harness, "probe"), action="land", ref="feature", review_override="-a dash first")
    assert parse(seen[seen.index("--root") :]).review_override == "-a dash first"


# ---------------------------------------------------------------- push-guard

ALPHA = "alpha-api"
PUSH = "git push origin agent/001-x"


def _push(cmd: str, cwd: Path) -> str:
    return json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(cwd),
            "tool_input": {"command": cmd},
        }
    )


def _commit(repo: Path, message: str) -> str:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message],
        cwd=repo,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def pushed(tmp_root: Path, run, notes_remote) -> tuple[Path, str]:
    """Alpha's `agent/001-x`, a worker branch whose dispatch names it (no worktree)."""
    assert run(["feature", "new", "x", "--title", "X"]).code == 0
    repo = tmp_root / "repos/alpha-api"
    subprocess.run(["git", "checkout", "-q", "-b", "agent/001-x"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    _dispatch(tmp_root, "001-01", repo=ALPHA, branch="agent/001-x")
    return repo, head.stdout.strip()


def _guard(run, cmd: str, cwd: Path) -> str:
    return run(["hook", "push-guard"], stdin=_push(cmd, cwd)).out


def _denied(run, cmd: str, cwd: Path) -> str:
    r = run(["hook", "push-guard"], stdin=_push(cmd, cwd))
    assert _decision(r) == "deny", (cmd, r.out)
    return _reason(r)


PUSH_CASES = [
    ({}, True),
    ({"blocking": ["F1"]}, False),
    ({"phase": "verifying", "blocking": None}, False),
    ({"head": "older"}, False),
    ({"branch": "agent/other"}, False),
    ({"repo": "beta-web"}, False),
]


def test_push_guard_needs_a_clean_round_on_the_pushed_head(pushed: tuple[Path, str], tmp_root: Path, run):
    repo, head = pushed
    reason = _denied(run, PUSH, repo)
    assert f"agent/001-x at {head[:12]}" in reason and "no code-review round has run on it" in reason
    assert 'code-review` with dispatch_id="001-01"' in reason and "AGENT_PUSH_UNREVIEWED=1" in reason
    # a finished review-row dispatch, the rule before ADR-0063, is not enough
    _dispatch(
        tmp_root,
        "001-02",
        repo=ALPHA,
        branch="agent/001-x",
        choice_reason="review row: read the branch",
        finished="2099-01-01T00:00:00-07:00",
        start_sha=head,
    )
    _denied(run, PUSH, repo)
    for over, allowed in PUSH_CASES:
        code_review.reviews_dir(tmp_root).mkdir(parents=True, exist_ok=True)
        for old in code_review.reviews_dir(tmp_root).glob("*.json"):
            old.unlink()
        at = "0" * 40 if over.get("head") == "older" else head
        rest = {"branch": "agent/001-x", "repo": ALPHA, **{k: v for k, v in over.items() if k != "head"}}
        _round(tmp_root, at, **rest)
        out = _guard(run, PUSH, repo)
        assert (out == "") is allowed, (over, out)


def test_push_guard_denies_a_malformed_round_beside_a_clean_one(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, head = pushed
    _round(tmp_root, head, branch="agent/001-x", repo=ALPHA)
    (code_review.reviews_dir(tmp_root) / f"{head}-2.json").write_text("{}", encoding="utf-8")
    assert "a malformed round record" in _denied(run, PUSH, repo)


def test_push_guard_checks_every_refspec(pushed: tuple[Path, str], tmp_root: Path, run):
    repo, head = pushed
    _round(tmp_root, head, branch="agent/001-x", repo=ALPHA)
    subprocess.run(["git", "checkout", "-q", "-b", "agent/001-y"], cwd=repo, check=True)
    later = _commit(repo, "unreviewed")
    _dispatch(tmp_root, "001-02", repo=ALPHA, branch="agent/001-y")
    assert _guard(run, PUSH, repo) == ""
    reason = _denied(run, "git push origin agent/001-x agent/001-y", repo)
    assert f"agent/001-y at {later[:12]}" in reason and "agent/001-x at" not in reason


def test_push_guard_strips_a_force_prefix_before_matching_records(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, head = pushed
    _commit(repo, "unreviewed")
    for cmd in ("git push origin +agent/001-x", "git push origin +refs/heads/agent/001-x"):
        assert "agent/001-x at" in _denied(run, cmd, repo)


def test_push_guard_checks_the_refspec_source_not_the_local_destination(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, head = pushed
    _round(tmp_root, head, branch="agent/001-x", repo=ALPHA)
    _dispatch(tmp_root, "001-01", repo=ALPHA, branch="agent/001-x", worktree=str(repo))
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True)
    _commit(repo, "unreviewed")
    assert _guard(run, PUSH, repo) == ""  # the local branch is the reviewed head
    for cmd in ("git push origin HEAD:agent/001-x", "git push origin +HEAD:refs/heads/agent/001-y"):
        assert "has no clean code-review round on that head" in _denied(run, cmd, repo)
    assert "detached head" in _denied(run, "git push origin HEAD", repo)


def test_push_guard_scopes_records_and_rounds_to_the_pushed_repo(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    """A dispatch or a clean round in another repo with the same branch name proves nothing here."""
    repo, head = pushed
    _dispatch(tmp_root, "001-00", repo="beta-web", branch="agent/001-x", feature=2)
    _round(tmp_root, head, branch="agent/001-x", repo="beta-web")
    reason = _denied(run, PUSH, repo)
    assert 'dispatch_id="001-01"' in reason


def test_push_guard_lets_a_renamed_branch_s_round_count_only_without_its_own(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, head = pushed
    _dispatch(tmp_root, "001-01", repo=ALPHA, branch="agent/001-x", worktree=str(repo))
    subprocess.run(["git", "branch", "feature/renamed"], cwd=repo, check=True)
    cmd = "git push origin feature/renamed"
    _round(tmp_root, head, 1, branch="agent/001-x", repo=ALPHA)
    assert _guard(run, cmd, repo) == ""
    _round(tmp_root, head, 2, branch="feature/renamed", repo=ALPHA, blocking=["F1"])
    assert "kept verified blocking findings: F1" in _denied(run, cmd, repo)


def test_push_guard_fails_open_on_an_unreadable_round_of_the_head(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, head = pushed
    base = code_review.reviews_dir(tmp_root)
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{head}-1.json").write_text("{not json", encoding="utf-8")
    assert _guard(run, PUSH, repo) == ""


def test_push_guard_override_notes_the_pushed_repo_s_feature(pushed: tuple[Path, str], tmp_root: Path, run):
    repo, head = pushed
    assert run(["feature", "new", "y", "--title", "Y"]).code == 0
    _dispatch(tmp_root, "001-00", repo="beta-web", branch="agent/001-x", feature=2)
    assert _guard(run, f"AGENT_PUSH_UNREVIEWED=1 {PUSH}", repo) == ""
    log = (tmp_root / "features/001-x/log.md").read_text()
    assert (
        f"pushed agent/001-x at {head[:12]} with AGENT_PUSH_UNREVIEWED=1 without a clean code-review round"
        in log
    )
    assert "no code-review round has run on it" in log
    other = tmp_root / "features/002-y/log.md"
    assert not other.exists() or "AGENT_PUSH_UNREVIEWED" not in other.read_text()


UNLISTED = [
    ("git push --all origin", "`--all` pushes refs the command does not name"),
    ("git push --mirror origin", "`--mirror`"),
    ("git push --tags origin", "`--tags`"),
    ("git push origin 'refs/heads/*:refs/heads/*'", "the glob refspec"),
    ("git push origin :", "the matching refspec `:`"),
    ("git push origin +:", "the matching refspec `:`"),
    ("git push", "no refspec"),
    ("git push origin", "no refspec"),
    ("git push --frobnicate origin agent/001-x", "the option `--frobnicate`"),
    ("git --git-dir=.git push origin agent/001-x", "the git option `--git-dir=.git`"),
]


@pytest.mark.parametrize(("cmd", "why"), UNLISTED)
def test_push_guard_fails_closed_on_a_push_it_cannot_list(pushed: tuple[Path, str], run, cmd: str, why: str):
    repo, head = pushed
    reason = _denied(run, cmd, repo)
    assert "cannot list exactly which refs this push sends" in reason and why in reason
    assert "`git push origin HEAD:agent/<name>`" in reason and "AGENT_PUSH_UNREVIEWED=1" in reason


def test_an_unlisted_push_with_the_override_passes_and_notes_the_worktree_s_feature(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, _ = pushed
    _dispatch(tmp_root, "001-01", repo=ALPHA, branch="agent/001-x", worktree=str(repo))
    assert _guard(run, "AGENT_PUSH_UNREVIEWED=1 git push --all origin", repo) == ""
    log = (tmp_root / "features/001-x/log.md").read_text()
    assert "a push whose refs push-guard cannot list (`--all` pushes refs the command does not name)" in log


def test_an_unlisted_push_outside_the_harness_and_registered_repos_is_not_guarded(tmp_root: Path):
    from agent_cli.hooks.repo_guards import push_guard

    loose = tmp_root.parent / "loose"
    loose.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=loose, check=True)
    payload = json.loads(_push("git push --all origin", loose))
    assert push_guard(tmp_root, payload) == ""


def test_push_guard_reads_a_repo_option_apart_from_the_refspecs(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, head = pushed
    subprocess.run(["git", "checkout", "-q", "-b", "agent/001-y"], cwd=repo, check=True)
    later = _commit(repo, "reviewed y")
    _dispatch(tmp_root, "001-02", repo=ALPHA, branch="agent/001-y")
    _round(tmp_root, later, branch="agent/001-y", repo=ALPHA)
    for cmd in (
        "git push --repo=origin agent/001-x agent/001-y",
        "git push --repo origin agent/001-x agent/001-y",
    ):
        reason = _denied(run, cmd, repo)
        assert f"agent/001-x at {head[:12]}" in reason and "agent/001-y at" not in reason


def test_push_guard_guards_a_ref_whose_source_a_dispatch_names(pushed: tuple[Path, str], tmp_root: Path, run):
    repo, head = pushed
    reason = _denied(run, "git push origin agent/001-x:feature/renamed", repo)
    assert f"feature/renamed at {head[:12]}" in reason
    _round(tmp_root, head, branch="agent/001-x", repo=ALPHA)
    assert _guard(run, "git push origin agent/001-x:feature/renamed", repo) == ""


def test_a_ref_only_another_repo_s_dispatch_names_is_not_guarded(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, _ = pushed
    subprocess.run(["git", "branch", "agent/001-z"], cwd=repo, check=True)
    _dispatch(tmp_root, "001-02", repo="beta-web", branch="agent/001-z", feature=2)
    assert _guard(run, "git push origin agent/001-z", repo) == ""
    assert _guard(run, "AGENT_PUSH_UNREVIEWED=1 git push origin agent/001-z", repo) == ""
    assert "AGENT_PUSH_UNREVIEWED" not in (tmp_root / "features/001-x/log.md").read_text()


def test_a_linked_worktree_outside_the_checkout_resolves_to_its_repo(
    pushed: tuple[Path, str], tmp_root: Path, run
):
    repo, _ = pushed
    assert run(["feature", "new", "y", "--title", "Y"]).code == 0
    linked = tmp_root.parent / "alpha-linked"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "agent/001-l", str(linked)], cwd=repo, check=True)
    head = _commit(linked, "linked work")
    _dispatch(tmp_root, "001-00", repo="beta-web", branch="agent/001-l", feature=2)
    _dispatch(tmp_root, "001-02", repo=ALPHA, branch="agent/001-l")
    _round(tmp_root, head, branch="agent/001-l", repo="beta-web")
    assert 'dispatch_id="001-02"' in _denied(run, "git push origin agent/001-l", linked)
    assert _guard(run, "AGENT_PUSH_UNREVIEWED=1 git push origin agent/001-l", linked) == ""
    assert "pushed agent/001-l" in (tmp_root / "features/001-x/log.md").read_text()
    assert "pushed agent/001-l" not in (tmp_root / "features/002-y/log.md").read_text()


@pytest.mark.parametrize("cmd", ["git push origin --delete agent/001-x", "git push origin :agent/001-x"])
def test_a_deletion_pushes_no_head(pushed: tuple[Path, str], tmp_root: Path, run, cmd: str):
    repo, _ = pushed
    assert _guard(run, cmd, repo) == ""
    assert _guard(run, f"AGENT_PUSH_UNREVIEWED=1 {cmd}", repo) == ""
    assert "AGENT_PUSH_UNREVIEWED" not in (tmp_root / "features/001-x/log.md").read_text()


def test_shell_control_after_the_push_is_not_a_refspec(pushed: tuple[Path, str], tmp_root: Path, run):
    repo, head = pushed
    _round(tmp_root, head, branch="agent/001-x", repo=ALPHA)
    for cmd in (f"{PUSH} && echo ok", f"{PUSH} 2>&1 | tail -n 5", f"{PUSH};ls"):
        assert _guard(run, cmd, repo) == "", cmd
