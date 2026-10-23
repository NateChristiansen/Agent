"""ADR-0063 in git's pre-push hook (`push_review`): every guarded ref git sends needs a clean
code-review round on its commit, on the harness and on a registered product clone, whatever the
shell spelled. The PreToolUse `push-guard` is the early warning (`test_code_review_gate.py`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_cli import code_review, gitgate, push_records, push_review
from agent_cli.push_input import Update
from land_helpers import git, harness  # noqa: F401 -- pytest fixture

ALPHA = "alpha-api"
BRANCH = "agent/001-x"
ZERO = "0" * 40
OVERRIDE = push_review.OVERRIDE_ENV


@dataclass
class Side:
    root: Path
    checkout: Path
    repo: str
    heads: dict[str, str] = field(default_factory=dict)

    def head(self, rev: str = BRANCH) -> str:
        """Cached until `commit`; a test that moves a ref another way reads it with `git`."""
        if rev not in self.heads:
            self.heads[rev] = git(self.checkout, "rev-parse", rev)
        return self.heads[rev]

    def commit(self, message: str) -> str:
        git(self.checkout, "commit", "-q", "--allow-empty", "-m", message)
        self.heads.clear()
        return self.head("HEAD")

    def dispatch(self, dispatch_id: str, **fields: Any) -> None:
        ddir = self.root / ".agent" / "dispatches"
        ddir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": dispatch_id,
            "feature": 1,
            "state": "done",
            "repo": self.repo,
            "branch": BRANCH,
            **fields,
        }
        (ddir / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def round(self, head: str, n: int = 1, *, branch: str = BRANCH, **over: Any) -> None:
        base = code_review.reviews_dir(self.root)
        base.mkdir(parents=True, exist_ok=True)
        rec = {
            "repo": self.repo,
            "branch": branch,
            "head": head,
            "round": n,
            "phase": "complete",
            "blocking": [],
            "launched_at": f"2026-10-{10 + n}T00:00:00+00:00",
            **over,
        }
        (base / f"{head}-{n}.json").write_text(json.dumps(rec), encoding="utf-8")

    def problems(self, *refs: tuple[str, str], env: dict[str, str] | None = None) -> list[str]:
        """The gate over (local rev, remote branch) pairs, as git would hand them over."""
        updates = [
            Update(self.head(rev), ZERO, f"refs/heads/{rev}", f"refs/heads/{dest}") for rev, dest in refs
        ]
        return push_review.problems(self.root, self.checkout, updates, env or {})


@pytest.fixture(params=["harness", "product"])
def side(request: pytest.FixtureRequest) -> Side:
    """`agent/001-x`, one commit ahead of main, which dispatch 001-01 names: in the harness
    itself, and in the product clone alpha whose hook reads the harness's records."""
    if request.param == "harness":
        root = request.getfixturevalue("harness")
        s = Side(root, root, "Harness")
    else:
        root = request.getfixturevalue("tmp_root")
        s = Side(root, root / "repos" / ALPHA, ALPHA)
    git(s.checkout, "switch", "-q", "-c", BRANCH)
    s.commit("work")
    s.dispatch("001-01")
    return s


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    from agent_cli import features, log

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(features, "resolve", lambda root, ref: f"feature {ref}")
    monkeypatch.setattr(log, "append", lambda f, kind, text: seen.append((f, text)))
    return seen


CASES = [
    ("clean", ""),
    ("missing", "no code-review round has run on it"),
    ("blocking", "round 1 kept verified blocking findings: F1"),
    ("running", "round 1 is still reviewing"),
    ("older", "its rounds are on older heads"),
    ("unreadable", "an unreadable round record"),
]


def test_a_clean_round_passes_and_every_other_head_is_refused(side: Side):
    head = side.head()
    base = code_review.reviews_dir(side.root)
    for case, why in CASES:
        for old in base.glob("*.json") if base.is_dir() else []:
            old.unlink()
        if case == "clean":
            side.round(head)
        elif case == "blocking":
            side.round(head, blocking=["F1"])
        elif case == "running":
            side.round(head, phase="reviewing", blocking=None)
        elif case == "older":
            side.round(side.head("main"))
        elif case == "unreadable":
            side.round(head)
            (base / f"{head}-2.json").write_text("{not json", encoding="utf-8")
        found = side.problems((BRANCH, BRANCH))
        if not why:
            assert found == [], (case, found)
            continue
        [text] = found
        assert f"{BRANCH} at {head} has no clean code-review round" in text and why in text, (case, text)
        assert 'the `dispatch` action `code-review` with dispatch_id="001-01"' in text, case
        assert f"set {OVERRIDE}=1 in the push's environment" in text, case


def test_the_override_needs_exactly_1_and_writes_its_note(side: Side, notes: list[tuple[str, str]]):
    head = side.head()
    for value in ("0", "true", " 1", "yes", ""):
        assert side.problems((BRANCH, BRANCH), env={OVERRIDE: value}), value
    assert notes == []
    assert side.problems((BRANCH, BRANCH), env={OVERRIDE: "1"}) == []
    [(feature, text)] = notes
    assert feature == "feature 1"
    assert f"pushed {BRANCH} at {head[:12]} with {OVERRIDE}=1 without a clean code-review round" in text
    assert "no code-review round has run on it" in text
    # with no feature to note, the override is refused
    side.dispatch("001-01", feature=None)
    [text] = side.problems((BRANCH, BRANCH), env={OVERRIDE: "1"})
    assert "honoured only with its note" in text and "names a feature" in text
    assert len(notes) == 1


def test_a_deletion_or_a_ref_only_another_repo_s_dispatch_names_is_not_guarded(
    side: Side, notes: list[tuple[str, str]]
):
    gone = Update(ZERO, side.head(), "(delete)", f"refs/heads/{BRANCH}")
    assert push_review.problems(side.root, side.checkout, [gone], {}) == []
    assert push_review.problems(side.root, side.checkout, [gone], {OVERRIDE: "1"}) == []
    assert notes == []
    git(side.checkout, "branch", "agent/001-z")
    side.dispatch("001-02", repo="beta-web", branch="agent/001-z", feature=2)
    assert side.problems(("agent/001-z", "agent/001-z")) == []
    assert side.problems(("agent/001-z", "agent/001-z"), env={OVERRIDE: "1"}) == []
    assert notes == []
    # main, which no dispatch names, is unchanged
    assert side.problems(("main", "main")) == []


def test_the_harness_is_the_harness_when_the_registry_does_not_list_it(harness: Path):
    """With no registry entry for the harness the pushed repo was unknown, so a product clone's
    dispatch naming the same branch guarded the harness's push."""
    (harness / "agent.yaml").write_text(
        "version: 1\nrepos:\n  - name: alpha-api\n    short: alpha\n    path: repos/alpha-api\n"
        "    remote: git@github.com:example/alpha.git\n    default_branch: main\n",
        encoding="utf-8",
    )
    s = Side(harness, harness, ALPHA)
    git(harness, "switch", "-q", "-c", BRANCH)
    s.commit("work")
    s.dispatch("001-01")
    assert s.problems((BRANCH, BRANCH)) == []
    s.dispatch("001-02", repo="Harness")
    assert len(s.problems((BRANCH, BRANCH))) == 1


def test_an_unreadable_registry_refuses_only_refs_a_dispatch_names(side: Side, notes: list[tuple[str, str]]):
    """The pushed repo is unknown, so a named ref is refused whatever its rounds say; `main`, a
    deletion and an unnamed ref pass, and the override still works with its note."""
    from agent_cli import config_file

    registry = config_file.registry_path(side.root)
    side.round(side.head())
    git(side.checkout, "branch", "topic/unnamed")
    gone = Update(ZERO, side.head(), "(delete)", f"refs/heads/{BRANCH}")
    for broken in ("malformed", "dangling"):
        registry.unlink()
        if broken == "malformed":
            registry.write_text("repos: [\n", encoding="utf-8")
        else:
            registry.symlink_to(side.root / "missing.yaml")
        notes.clear()
        assert side.problems(("main", "main")) == [], broken
        assert side.problems(("topic/unnamed", "topic/unnamed")) == [], broken
        assert push_review.problems(side.root, side.checkout, [gone], {}) == [], broken
        [text] = side.problems((BRANCH, BRANCH))
        assert "the registry could not be read" in text and f"set {OVERRIDE}=1" in text, (broken, text)
        assert notes == [], broken
        assert side.problems((BRANCH, BRANCH), env={OVERRIDE: "1"}) == [], broken
        [(_, note)] = notes
        assert f"pushed {BRANCH}" in note and "the registry could not be read" in note, broken


def test_several_refs_in_one_push_are_each_checked(side: Side):
    """A reviewed ref, an unreviewed one, the reviewed commit pushed under a name with no round of
    its own, and a guarded ref whose object is no commit: one line per refused ref. The round
    counts under the pushed branch's name only."""
    head = side.head()
    side.round(head)
    git(side.checkout, "switch", "-q", "-c", "agent/001-y")
    later = side.commit("unreviewed")
    side.dispatch("001-02", branch="agent/001-y")
    found = side.problems((BRANCH, BRANCH), ("agent/001-y", "agent/001-y"), (BRANCH, "feature/renamed"))
    assert len(found) == 2 and f"agent/001-y at {later}" in found[0], found
    assert f"feature/renamed at {head}" in found[1] and "no code-review round has run on it" in found[1]
    side.round(head, 2, branch="feature/renamed")
    assert side.problems((BRANCH, "feature/renamed")) == []
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=side.checkout,
        input="x\n",
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = Update(blob, ZERO, "refs/heads/agent/001-y", "refs/heads/agent/001-y")
    [text] = push_review.problems(side.root, side.checkout, [tree], {})
    assert "not a commit this checkout can read" in text


CALL_RE = re.compile(
    r'`code-review` with branch="([^"]+)", base="([^"]+)", paths=\["([^"]+)"\], repo="([^"]+)"'
)


def test_a_refusal_names_the_call_that_makes_the_pushed_ref_clean(side: Side):
    """A pushed name other than the dispatch's branch, a renamed destination or a tag pushed from
    its worktree, has its round under that name. The refusal names that `code-review` call, and
    first the ref it needs when the name does not resolve to the pushed commit in the clone."""
    from agent_cli.ops import code_review as review_op

    head = side.head()
    order = side.root / "features" / "001-x" / "orders" / "001-01.md"
    order.parent.mkdir(parents=True, exist_ok=True)
    order.write_text("# acceptance\n", encoding="utf-8")
    side.dispatch("001-01", worktree=str(side.checkout), start_sha=side.head("main"), work_order=str(order))
    side.round(head)
    for n, shape in enumerate(("renamed", "tag"), start=2):
        if shape == "renamed":
            name = "release/renamed"
            update = Update(head, ZERO, f"refs/heads/{BRANCH}", f"refs/heads/{name}")
        else:
            git(side.checkout, "tag", "-a", "-m", "release", "v1", head)
            name = "refs/tags/v1"
            update = Update(side.head("v1"), ZERO, name, name)
        [text] = push_review.problems(side.root, side.checkout, [update], {})
        branch, base, path, repo = CALL_RE.search(text).groups()  # type: ignore[union-attr]
        assert (branch, base, path) == (name, side.head("main"), str(order)), text
        full = f"refs/heads/{name}" if shape == "renamed" else name
        assert (f"git update-ref {full} {head}" in text) is (shape == "renamed"), text
        if shape == "renamed":
            git(side.checkout, "update-ref", full, head)
        subject = review_op.subject(
            side.root, dispatch_id=None, branch=branch, base=base, paths=[path], repo=repo
        )
        assert (subject.branch, review_op._commit(subject.checkout, subject.branch, "branch")) == (name, head)
        side.round(head, n, branch=subject.branch, repo=subject.repo)
        assert push_review.problems(side.root, side.checkout, [update], {}) == [], shape


def test_the_hook_entry_points_run_the_gate(side: Side, run, monkeypatch: pytest.MonkeyPatch):
    """The harness's `git pre-push` runs it after the pass-record gate; a product clone's hook
    runs `git push-review <checkout>`."""
    monkeypatch.setattr(push_records, "problems", lambda root, updates: [])
    stdin = f"refs/heads/{BRANCH} {side.head()} refs/heads/{BRANCH} {ZERO}\n"
    argv = ["git", "pre-push"] if side.checkout == side.root else ["git", "push-review", str(side.checkout)]
    refused = run(["--root", str(side.root), *argv, "origin", "url"], stdin=stdin)
    assert refused.code != 0 and push_review.REFUSED in refused.err and f"{BRANCH} at" in refused.err
    side.round(side.head())
    assert run(["--root", str(side.root), *argv, "origin", "url"], stdin=stdin).code == 0


# ---------------------------------------------------------------- end to end, through git


@pytest.fixture
def hooked(tmp_root: Path, run, notes_remote: Path) -> Side:
    """Alpha with the installed hook, and dispatch 001-01's worktree, a linked worktree of alpha
    on `agent/001-x` that shares the hook through alpha's git dir. Pushes go to alpha's bare
    remote."""
    assert run(["feature", "new", "x", "--title", "X"]).code == 0
    alpha = tmp_root / "repos" / ALPHA
    assert gitgate.install_review(tmp_root)["alpha"] == "installed"
    worktree = tmp_root.parent / "wt-001"
    git(alpha, "worktree", "add", "-q", "-b", BRANCH, str(worktree))
    s = Side(tmp_root, worktree, ALPHA)
    s.commit("work")
    s.dispatch("001-01", worktree=str(worktree))
    return s


SH = shutil.which("sh") or r"C:\Program Files\Git\bin\sh.exe"


def _shell(cwd: Path, command: str) -> subprocess.CompletedProcess[str]:
    """`command` as the Bash tool would run it: through a shell, which expands and unquotes."""
    return subprocess.run([SH, "-c", command], cwd=cwd, capture_output=True, text=True, check=False)


def _guard(root: Path, command: str, cwd: Path) -> str:
    from agent_cli.hooks.repo_guards import push_guard

    payload = {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}
    return push_guard(root, payload)


def test_round_4_s_shapes_are_decided_by_the_hook(hooked: Side):
    """Each shell spelling that push-guard misread, run by a real shell, is one exact ref list to
    git. push-guard now reads the first two (quoted `-C`, override 0); the others it cannot list
    pass it, and the hook decides. A refused push changes nothing, so the shapes share one clone."""
    s, wt = hooked, hooked.checkout
    head = s.head()
    log = s.root / "features" / "001-x" / "log.md"

    def refused(command: str, cwd: Path = wt, *, denied: bool = False) -> str:
        guarded = _guard(s.root, command, cwd)
        assert ('"deny"' in guarded) is denied, (command, guarded)
        out = _shell(cwd, command)
        assert out.returncode != 0 and push_review.REFUSED in out.stderr, (command, out.stderr)
        return out.stderr

    assert f"{BRANCH} at {head}" in refused(
        f'git -C "{wt.as_posix()}" push origin {BRANCH}', s.root, denied=True
    )
    assert f"{BRANCH} at {head}" in refused(f"{OVERRIDE}=0 git push origin {BRANCH}", denied=True)
    assert f"{BRANCH} at {head}" in refused(f'B={BRANCH}; git push origin "$B"')
    assert OVERRIDE not in log.read_text(encoding="utf-8")
    # --all, refused, then pushed with the override: one note per guarded ref git sends
    git(wt, "branch", "agent/001-y")
    s.dispatch("001-02", branch="agent/001-y")
    stderr = refused("git push --all origin")
    assert f"{BRANCH} at {head}" in stderr and f"agent/001-y at {head}" in stderr
    assert _shell(wt, f"{OVERRIDE}=1 git push --all origin").returncode == 0
    notes = log.read_text(encoding="utf-8")
    assert f"pushed {BRANCH} at {head[:12]} with {OVERRIDE}=1" in notes
    assert f"pushed agent/001-y at {head[:12]} with {OVERRIDE}=1" in notes
    assert "pushed main" not in notes
    # follow tags: the reviewed tip passes, the annotated tag on the unreviewed commit is refused
    git(wt, "tag", "-a", "-m", "release", "v1", head)
    s.round(s.commit("reviewed tip"))
    stderr = refused(f"git -c push.followTags=true push origin {BRANCH}")
    tag = git(wt, "rev-parse", "v1")
    assert f"refs/tags/v1 at {tag} (commit {head})" in stderr, stderr
    assert f"{BRANCH} at" not in stderr
    assert log.read_text(encoding="utf-8") == notes


SKIPPED_SPECS = [
    "--all origin",
    "",
    "origin 'refs/heads/*:refs/heads/*'",
    'origin "$B"',
    f"-c push.followTags=true push origin {BRANCH}",
]


def test_an_override_cannot_skip_pre_push_so_pre_push_writes_its_note(hooked: Side):
    """A push that skipped pre-push left push-guard to write the override note, and it misread the
    refs: an annotated tag `push.followTags` sent, a branch behind `"$B"`. The hook-bypass guard now
    denies the skip, and pre-push notes every ref git sends. Each spec pushes a new unreviewed
    commit; the tag stays unpushed until the `push.followTags` spec, the last."""
    from agent_cli.hooks.commit_guard import hook_bypass_guard

    s, wt = hooked, hooked.checkout
    log = s.root / "features" / "001-x" / "log.md"
    # read literally, `"HEAD:$B"` named a branch `$B` with no round, so push-guard denied a push
    # whose expanded destination has a clean round and passes pre-push
    s.round(s.head(), branch="release/x")
    command = 'B=release/x; git push origin "HEAD:$B"'
    assert _guard(s.root, command, wt) == ""
    pushed = _shell(wt, command)
    assert pushed.returncode == 0, pushed.stderr
    git(wt, "config", "push.default", "current")
    git(wt, "tag", "-a", "-m", "release", "v1", s.head())
    for n, spec in enumerate(SKIPPED_SPECS):
        head = s.commit(f"unreviewed {n}")
        if spec.startswith("-c"):
            command = f"{OVERRIDE}=1 git {spec}"
            skipping = f"{OVERRIDE}=1 git {spec.replace(' push ', ' push --no-verify ')}"
        else:
            command = f"B={BRANCH}; {OVERRIDE}=1 git push {spec}".rstrip()
            skipping = f"B={BRANCH}; {OVERRIDE}=1 git push --no-verify {spec}".rstrip()
        for skip in (skipping, f"B={BRANCH}; git -c core.hooksPath=/dev/null push origin {BRANCH}"):
            payload = {"tool_name": "Bash", "cwd": str(wt), "tool_input": {"command": skip}}
            assert "let the hook run" in hook_bypass_guard(s.root, payload), skip
        assert _guard(s.root, command, wt) == "", spec
        before = log.read_text(encoding="utf-8")
        pushed = _shell(wt, command)
        assert pushed.returncode == 0, (spec, pushed.stderr)
        notes = log.read_text(encoding="utf-8").removeprefix(before)
        assert f"pushed {BRANCH} at {head[:12]} with {OVERRIDE}=1" in notes, (spec, notes)
        assert ("pushed refs/tags/v1" in notes) is spec.startswith("-c"), (spec, notes)


def test_a_linked_worktree_of_a_later_registered_clone_is_checked_in_its_own_repo(tmp_root: Path):
    """Git hands a hook run from a linked worktree its `GIT_DIR`. Inherited, it answered every
    registered checkout's common-dir query with the pusher's, so beta's push matched alpha, the
    first entry, and beta's dispatch no longer guarded it."""
    beta = tmp_root / "repos" / "beta-web"
    beta.mkdir()
    bare = tmp_root.parent / "beta-remote.git"
    git(tmp_root.parent, "init", "-q", "--bare", "-b", "main", str(bare))
    git(beta, "init", "-q", "-b", "main")
    git(beta, "commit", "-q", "--allow-empty", "-m", "base")
    git(beta, "remote", "add", "origin", str(bare))
    assert gitgate.install_review(tmp_root)["beta"] == "installed"
    linked = tmp_root.parent / "beta-linked"
    git(beta, "worktree", "add", "-q", "-b", "agent/002-b", str(linked))
    s = Side(tmp_root, linked, "beta-web")
    head = s.commit("beta work")
    s.dispatch("002-01", branch="agent/002-b", feature=2)
    refused = _shell(linked, "git push origin agent/002-b")
    assert refused.returncode != 0 and f"agent/002-b at {head}" in refused.stderr, refused.stderr
