"""`notes_sync` (ADR-0034): pull at session start, commit and publish at Stop.

Real git throughout: a bare remote and two clones of it, `a` (this machine, linked to the
harness root) and `b` (the other machine). Nothing here touches the owner's notes repo.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_cli import config_file, notes_sync
from harness_root import repoint_files


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@dataclass
class Pair:
    harness: Path
    remote: Path
    a: Path
    b: Path

    def mode(self, value: str) -> None:
        (self.harness / "agent.local.yaml").write_text(f"notes_sync: {value}\n", encoding="utf-8")

    def commit(self, clone: Path, name: str, text: str, push: bool = False) -> None:
        (clone / name).write_text(text, encoding="utf-8")
        git(clone, "add", name)
        git(clone, "commit", "-q", "-m", f"edit {name}")
        if push:
            git(clone, "push", "-q", "origin", "HEAD:main")


REPOS = ("remote.git", "notes-a", "notes-b")


_TEMPLATE: list[Path] = []


def _pair_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The remote and both seeded clones, built once per worker and copied per test. A plain
    function, not a fixture, so modules that import only `pair` still get it."""
    if _TEMPLATE:
        return _TEMPLATE[0]
    base = tmp_path_factory.mktemp("notes-pair")
    empty = base / "gitconfig"
    empty.write_text("", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GIT_CONFIG_GLOBAL", str(empty))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        remote = base / "remote.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(remote))
        clones = []
        for name in REPOS[1:]:
            git(base, "clone", "-q", str(remote), name)
            clone = base / name
            git(clone, "config", "user.name", "t")
            git(clone, "config", "user.email", "t@t")
            clones.append(clone)
        a, b = clones
        (a / "seed.md").write_text("seed\n", encoding="utf-8")
        git(a, "add", "seed.md")
        git(a, "commit", "-q", "-m", "seed")
        git(a, "push", "-q", "-u", "origin", "main")
        git(b, "pull", "-q", "origin", "main")
        git(b, "branch", "-q", "--set-upstream-to=origin/main")
    _TEMPLATE.append(base)
    return base


@pytest.fixture
def pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Pair:
    template = _pair_template(tmp_path_factory)
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in REPOS:
        shutil.copytree(template / name, tmp_path / name, symlinks=True)
    repoint_files(tmp_path, template, tuple(f"{name}/.git/config" for name in REPOS[1:]))
    remote, a, b = (tmp_path / name for name in REPOS)
    harness = tmp_path / "harness"
    (harness / ".agent").mkdir(parents=True)
    (harness / ".agent" / "notes-root").write_text(str(a), encoding="utf-8")
    p = Pair(harness, remote, a, b)
    p.mode("on")
    return p


@pytest.fixture
def told(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    from agent_cli.telemetry import attention

    seen: list[dict[str, object]] = []
    monkeypatch.setattr(attention, "emit", lambda root, kind, **data: seen.append({"kind": kind, **data}))
    return seen


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every git argv notes_sync runs, in order."""
    seen: list[list[str]] = []
    real = notes_sync.procs.run_bounded

    def spy(cmd, **kw):
        seen.append(list(cmd)[1:])
        return real(cmd, **kw)

    monkeypatch.setattr(notes_sync.procs, "run_bounded", spy)
    return seen


def verbs(seen: list[list[str]]) -> list[str]:
    return [c[0] for c in seen]


def state(pair: Pair) -> dict:
    return notes_sync._read_state(pair.a / ".git")


# ------------------------------------------------------------------ the switch


@pytest.mark.parametrize(
    ("text", "mode"), [("on", "on"), ("off", "off"), ("pull", "pull"), ("'on'", "on"), ("maybe", "off")]
)
def test_the_switch_reads_yaml_booleans_and_defaults_off(tmp_path: Path, text: str, mode: str):
    (tmp_path / "agent.local.yaml").write_text(f"notes_sync: {text}\n", encoding="utf-8")
    assert config_file.notes_sync_mode(tmp_path) == mode
    assert config_file.notes_sync_mode(tmp_path / "missing") == "off"


def test_the_switch_sits_beside_repo_paths_in_the_local_file(tmp_path: Path):
    (tmp_path / "agent.local.yaml").write_text(
        "notes_sync: pull\nrepos:\n  web:\n    path: /x/web\n", encoding="utf-8"
    )
    assert config_file.load_local(tmp_path, {"web"}) == {"web": {"path": str(Path("/x/web"))}}
    (tmp_path / "agent.local.yaml").write_text("notes_sync: on\n", encoding="utf-8")
    assert config_file.load_local(tmp_path, set()) == {}


def test_the_clear_command_is_wired_and_says_when_there_is_no_halt(pair: Pair, run):
    (pair.harness / "bin").mkdir()
    (pair.harness / "bin" / "agent").write_text("", encoding="utf-8")
    r = run(["--root", str(pair.harness), "notes-sync", "clear"])
    assert r.code == 0, r.err
    assert "no halt was recorded" in r.out


def test_off_runs_no_git_command(pair: Pair, calls):
    pair.mode("off")
    assert notes_sync.pull(pair.harness).status == "off"
    assert notes_sync.sync(pair.harness).status == "off"
    assert calls == []


def test_pull_mode_never_commits_at_stop(pair: Pair):
    pair.mode("pull")
    (pair.a / "new.md").write_text("x\n", encoding="utf-8")
    assert notes_sync.sync(pair.harness).status == "off"
    assert git(pair.a, "status", "--porcelain") == "?? new.md"


def test_no_notes_root_does_nothing(pair: Pair):
    (pair.harness / ".agent" / "notes-root").unlink()
    assert notes_sync.pull(pair.harness).status == "none"


# ------------------------------------------------------------------ pull


def test_pull_fast_forwards_reports_the_count_and_tells_all_but_current(
    pair: Pair, told: list[dict[str, object]]
):
    """Views re-render only when commits came in, from a pull or a sync."""
    pair.commit(pair.b, "from-b.md", "b\n", push=True)
    r = notes_sync.pull(pair.harness)
    assert (r.status, r.count, r.incoming) == ("pulled", 1, True)
    assert (pair.a / "from-b.md").read_text(encoding="utf-8") == "b\n"
    r = notes_sync.pull(pair.harness)  # current: kept as an attempt, never told
    assert r.status == "current" and not r.incoming
    assert [(e["kind"], e["op"], e["status"], e["count"]) for e in told] == [
        ("notes.sync", "pull", "pulled", 1)
    ]
    kept = notes_sync.last_outcomes(pair.harness)
    assert kept["last_attempt"]["status"] == "current" and kept["last_ok"]["status"] == "current"
    assert "last_error" not in kept


def test_pull_with_a_dirty_tree_changes_nothing(pair: Pair):
    pair.commit(pair.b, "seed.md", "remote\n", push=True)
    (pair.a / "staged.md").write_text("staged\n", encoding="utf-8")
    git(pair.a, "add", "staged.md")
    (pair.a / "seed.md").write_text("unstaged\n", encoding="utf-8")
    head, status = git(pair.a, "rev-parse", "HEAD"), git(pair.a, "status", "--porcelain")
    assert notes_sync.pull(pair.harness).status == "deferred"
    assert git(pair.a, "rev-parse", "HEAD") == head
    assert git(pair.a, "status", "--porcelain") == status
    assert (pair.a / "seed.md").read_text(encoding="utf-8") == "unstaged\n"


def test_pull_offline_keeps_head_and_records_a_failure_that_keeps_its_id(pair: Pair):
    git(pair.a, "remote", "set-url", "origin", str(pair.remote.parent / "gone.git"))
    head = git(pair.a, "rev-parse", "HEAD")
    r = notes_sync.pull(pair.harness)
    assert r.status == "offline"
    assert git(pair.a, "rev-parse", "HEAD") == head
    assert state(pair)["failure"]["status"] == "offline"
    first = state(pair)["failure"]["id"]
    notes_sync.pull(pair.harness)
    assert state(pair)["failure"]["id"] == first
    standing = notes_sync.standing_failure(pair.harness)
    assert standing is not None and standing["id"] == first


def test_pull_rebases_a_diverged_clean_tree_without_pushing(pair: Pair):
    pair.commit(pair.b, "from-b.md", "b\n", push=True)
    pair.commit(pair.a, "from-a.md", "a\n")
    r = notes_sync.pull(pair.harness)
    assert r.status == "pulled"
    assert (pair.a / "from-b.md").exists() and (pair.a / "from-a.md").exists()
    assert git(pair.a, "rev-list", "--count", "@{u}..HEAD") == "1"


# ------------------------------------------------------------------ telling


def test_an_error_that_a_later_sync_clears_still_reaches_doctor(pair: Pair, told: list[dict[str, object]]):
    """Earlier, doctor saw only a standing failure; an error a later success cleared, or one
    that never stood, was seen by nobody."""
    from agent_cli.doctor import checks_notes

    url = git(pair.a, "remote", "get-url", "origin")
    git(pair.a, "remote", "set-url", "origin", str(pair.remote.parent / "gone.git"))
    assert notes_sync.pull(pair.harness).status == "offline"
    assert told[-1]["status"] == "offline" and "fetch failed" in str(told[-1]["detail"])
    [row] = checks_notes.check_notes_sync(pair.harness)
    assert row["level"] == "warn" and "offline" in row["detail"]  # the standing failure
    git(pair.a, "remote", "set-url", "origin", url)
    assert notes_sync.pull(pair.harness).status == "current"
    [row] = checks_notes.check_notes_sync(pair.harness)
    assert (
        row["level"] == "ok" and "last error pull offline" in row["detail"] and "recovered" in row["detail"]
    )


def test_an_exception_inside_the_core_is_told_as_an_error(pair: Pair, told, monkeypatch):
    def boom(repo: object) -> str:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(notes_sync, "upstream_of", boom)
    r = notes_sync.pull(pair.harness)
    assert r.status == "error"
    assert told[-1]["status"] == "error" and "RuntimeError" in str(told[-1]["detail"])
    assert notes_sync.last_outcomes(pair.harness)["last_error"]["op"] == "pull"


def test_outcomes_are_kept_where_git_says_when_discovery_cannot_tell(pair: Pair, told, monkeypatch):
    """Windows has no `os.getuid`, so the ownership check never passes there and `common_dir`
    always defers to git."""
    from agent_cli import git_discovery

    monkeypatch.setattr(git_discovery, "common_dir", lambda start: None)
    assert notes_sync.pull(pair.harness).status == "current"
    assert notes_sync.last_outcomes(pair.harness)["last_attempt"]["status"] == "current"
    assert (pair.a / ".git" / notes_sync.SYNC_DIR / notes_sync.LAST_FILE).is_file()


def test_the_lock_follows_the_os_not_a_patched_platform_string(pair: Pair, monkeypatch):
    """A test that fakes `sys.platform` (the macOS notify channel does) patches it for every
    module, and the lock must still use what this OS has."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin" if sys.platform == "win32" else "win32")
    common = pair.a / ".git"
    with notes_sync.repo_lock(common, 1.0), pytest.raises(TimeoutError):
        with notes_sync.repo_lock(common, 0.1):
            pass


def test_off_tells_and_keeps_nothing(pair: Pair, told):
    pair.mode("off")
    notes_sync.pull(pair.harness)
    notes_sync.sync(pair.harness)
    assert told == [] and notes_sync.last_outcomes(pair.harness) == {}


# ------------------------------------------------------------------ sync


def test_a_turn_that_changed_nothing_makes_no_commit_and_no_network_call(pair: Pair, calls):
    head = git(pair.a, "rev-parse", "HEAD")
    assert notes_sync.sync(pair.harness).status == "current"
    assert git(pair.a, "rev-parse", "HEAD") == head
    assert "fetch" not in verbs(calls) and "push" not in verbs(calls) and "commit" not in verbs(calls)


def test_sync_commits_every_kind_of_change_and_pushes(pair: Pair):
    pair.commit(pair.a, "gone.md", "g\n")
    pair.commit(pair.a, "old.md", "o\n")
    git(pair.a, "push", "-q")
    git(pair.a, "rm", "-q", "gone.md")
    git(pair.a, "mv", "old.md", "new.md")
    (pair.a / "seed.md").write_text("staged\n", encoding="utf-8")
    git(pair.a, "add", "seed.md")
    (pair.a / "seed.md").write_text("staged then edited\n", encoding="utf-8")
    (pair.a / "untracked.md").write_text("u\n", encoding="utf-8")
    session = {"name": "mgr-011-notes-auto-sync", "feature": {"id": 11, "slug": "notes-auto-sync"}}
    r = notes_sync.sync(pair.harness, session)
    assert r.status == "pushed"
    assert git(pair.a, "status", "--porcelain") == ""
    assert git(pair.a, "rev-parse", "HEAD") == git(pair.remote, "rev-parse", "main")
    # the subject names what was staged; the session is in the body
    assert (
        git(pair.a, "log", "-1", "--format=%s")
        == "notes: auto-sync gone.md (1), new.md (1), seed.md (1), untracked.md (1)"
    )
    assert "Stop of session mgr-011-notes-auto-sync (011-notes-auto-sync) on " in git(
        pair.a, "log", "-1", "--format=%b"
    )
    assert git(pair.remote, "show", "main:seed.md") == "staged then edited"
    names = set(git(pair.remote, "ls-tree", "--name-only", "main").splitlines())
    assert names == {"seed.md", "new.md", "untracked.md"}


def test_git_never_prompts_for_credentials(pair: Pair, monkeypatch):
    """Windows clones use HTTPS through Git Credential Manager, which would open a sign-in
    window from a hook (found on a Windows machine, 2026-10-03). An owner's own GCM setting stands."""
    monkeypatch.delenv("GCM_INTERACTIVE", raising=False)
    env = notes_sync.Repo(pair.a, 1).env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    monkeypatch.setenv("GCM_INTERACTIVE", "auto")
    assert notes_sync.Repo(pair.a, 1).env["GCM_INTERACTIVE"] == "auto"


def test_the_message_counts_files_in_plain_english(tmp_path: Path):
    session = {"name": "mgr-x", "feature": {"id": 11, "slug": "notes-auto-sync"}}
    assert notes_sync._message(tmp_path, session, ["a.md"])[1].endswith(". 1 file.")
    assert notes_sync._message(tmp_path, session, ["a.md", "b.md"])[1].endswith(". 2 files.")
    assert "(unmapped)" in notes_sync._message(tmp_path, None, ["a.md"])[1]


def test_the_subject_names_what_was_staged_not_the_stop_session(tmp_path: Path):
    """Stop stages every manager's writes, so the session's feature mislabelled
    42% of the window's commits. The subject is built from the staged paths; the session and the
    configured machine label go in the body."""
    (tmp_path / "agent.local.yaml").write_text("machine: mac\n", encoding="utf-8")
    session = {"name": "mgr-009", "feature": {"id": 9, "slug": "launch"}}
    staged = [
        "features/045-acl/log.md",
        "features/045-acl/handoffs/001-mac-09.md",
        "features/059-win/plan.md",
        "initiatives/01-x/features/061-nested/plan.md",
        "journal/2026-10-12.md",
    ]
    subject, body = notes_sync._message(tmp_path, session, staged)
    assert subject == "notes: auto-sync 045 (2), 059 (1), 061 (1), journal (1)"
    assert "009" not in subject
    assert body == "Stop of session mgr-009 (009-launch) on mac. 5 files."


def test_a_long_subject_counts_the_rest():
    groups = notes_sync.staged_groups([f"features/{n:03d}-x/log.md" for n in range(1, 10)])
    assert len(groups) == 9
    paths = [f"features/{n:03d}-x/log.md" for n in range(1, 10)]
    subject, _ = notes_sync._message(Path("/nonexistent"), None, paths)
    assert subject.endswith(", +3 more")


def test_sync_publishes_a_clean_tree_that_is_ahead(pair: Pair):
    pair.commit(pair.a, "manual.md", "m\n")  # a manager's own commit, or one made offline
    assert notes_sync.sync(pair.harness).status == "pushed"
    assert git(pair.remote, "rev-parse", "main") == git(pair.a, "rev-parse", "HEAD")


def test_sync_rebases_when_the_other_machine_pushed_first_and_marks_incoming(pair: Pair):
    pair.commit(pair.b, "from-b.md", "b\n", push=True)
    (pair.a / "from-a.md").write_text("a\n", encoding="utf-8")
    r = notes_sync.sync(pair.harness)
    assert (r.status, r.incoming) == ("pushed", True)
    names = set(git(pair.remote, "ls-tree", "--name-only", "main").splitlines())
    assert {"from-a.md", "from-b.md"} <= names
    (pair.a / "more.md").write_text("m\n", encoding="utf-8")
    r = notes_sync.sync(pair.harness)
    assert (r.status, r.incoming) == ("pushed", False)


def test_an_empty_index_after_a_race_is_no_commit_and_no_failure(pair: Pair, monkeypatch):
    real = notes_sync.Repo.out

    def out(self, *args, cleanup=False):
        if args == ("status", "--porcelain"):
            return " M seed.md"  # changed when looked at, reverted before `git add`
        return real(self, *args, cleanup=cleanup)

    monkeypatch.setattr(notes_sync.Repo, "out", out)
    head = git(pair.a, "rev-parse", "HEAD")
    assert notes_sync.sync(pair.harness).status == "current"
    assert git(pair.a, "rev-parse", "HEAD") == head
    assert "failure" not in state(pair)


def test_a_push_remote_elsewhere_is_a_configuration_result(pair: Pair, calls):
    git(pair.a, "config", "branch.main.pushRemote", "mirror")
    (pair.a / "x.md").write_text("x\n", encoding="utf-8")
    r = notes_sync.sync(pair.harness)
    assert r.status == "config" and "mirror" in r.detail
    assert "push" not in verbs(calls) and "commit" not in verbs(calls)


def test_a_detached_head_is_a_configuration_result(pair: Pair):
    git(pair.a, "checkout", "-q", "--detach")
    assert notes_sync.pull(pair.harness).status == "config"


# ------------------------------------------------------------------ conflicts and halts


def conflict(pair: Pair) -> str:
    pair.commit(pair.b, "seed.md", "from b\n", push=True)
    (pair.a / "seed.md").write_text("from a\n", encoding="utf-8")
    r = notes_sync.sync(pair.harness)
    assert r.status == "conflict", r
    return git(pair.a, "rev-parse", "HEAD")


def test_a_conflict_keeps_the_local_commit_and_retries_only_on_a_new_upstream_commit(pair: Pair, calls):
    head = conflict(pair)
    assert git(pair.a, "show", "HEAD:seed.md") == "from a"
    assert not (pair.a / ".git" / "rebase-merge").exists()
    assert git(pair.a, "status", "--porcelain") == ""
    assert state(pair)["conflict"]["local"] == head
    calls.clear()
    assert notes_sync.sync(pair.harness).status == "conflict"
    assert "rebase" not in verbs(calls) and "push" not in verbs(calls)
    assert notes_sync.pull(pair.harness).status == "conflict"
    assert "rebase" not in verbs(calls)
    pair.commit(pair.b, "other.md", "o\n", push=True)
    calls.clear()
    notes_sync.sync(pair.harness)
    assert "rebase" in verbs(calls)


def test_a_failed_abort_halts_everything_until_the_owner_clears_it(pair: Pair, monkeypatch, calls):
    real = notes_sync.Repo.git

    def git_without_abort(self, *args, cap=notes_sync.LOCAL_CAP_S, cleanup=False):
        if args[:2] == ("rebase", "--abort"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "abort refused")
        return real(self, *args, cap=cap, cleanup=cleanup)

    monkeypatch.setattr(notes_sync.Repo, "git", git_without_abort)
    pair.commit(pair.b, "seed.md", "from b\n", push=True)
    (pair.a / "seed.md").write_text("from a\n", encoding="utf-8")
    r = notes_sync.sync(pair.harness)
    assert r.status == "halted" and "agent notes-sync clear" in r.detail
    calls.clear()
    assert notes_sync.pull(pair.harness).status == "halted"
    assert notes_sync.sync(pair.harness).status == "halted"
    assert not {"fetch", "add", "commit", "rebase", "push", "merge"} & set(verbs(calls))
    with pytest.raises(notes_sync.core.AgentError, match="in progress"):
        notes_sync.clear_halt(pair.harness)
    git(pair.a, "rebase", "--abort")
    assert notes_sync.clear_halt(pair.harness) == "halt cleared; the next trigger syncs again"
    assert "halt" not in state(pair)


def test_a_failure_survives_off_and_clears_only_on_convergence(pair: Pair):
    url = git(pair.a, "remote", "get-url", "origin")
    git(pair.a, "remote", "set-url", "origin", str(pair.remote.parent / "gone.git"))
    pair.commit(pair.a, "a.md", "a\n")
    assert notes_sync.sync(pair.harness).status == "offline"
    pair.mode("off")
    notes_sync.sync(pair.harness)
    assert state(pair)["failure"]["status"] == "offline"
    pair.mode("on")
    git(pair.a, "remote", "set-url", "origin", url)
    assert notes_sync.sync(pair.harness).status == "pushed"
    assert "failure" not in state(pair)


# ------------------------------------------------------------------ lock and budget


def test_a_held_lock_skips_and_two_harness_roots_share_it(pair: Pair, monkeypatch, tmp_path: Path):
    other = tmp_path / "harness-2"
    (other / ".agent").mkdir(parents=True)
    (other / ".agent" / "notes-root").write_text(str(pair.a), encoding="utf-8")
    (other / "agent.local.yaml").write_text("notes_sync: on\n", encoding="utf-8")
    monkeypatch.setattr(notes_sync, "LOCK_WAIT_S", 0.2)
    with notes_sync.repo_lock(pair.a / ".git", 1):
        assert notes_sync.sync(other).status == "locked"


def test_the_lock_wait_counts_against_the_budget(pair: Pair, monkeypatch):
    """The budget is cut to 0.3 s once the repo is open, not before: a loaded machine ran the
    `git rev-parse` that opens it past a 0.3 s whole-pull budget, and the pull said `none`."""
    real_open, real_lock, waits = notes_sync.open_repo, notes_sync.repo_lock, []

    def open_then_spend(root, budget):
        opened = real_open(root, budget)
        if not isinstance(opened, notes_sync.Result):
            opened[0].end = time.monotonic() + 0.3
        return opened

    monkeypatch.setattr(notes_sync, "open_repo", open_then_spend)
    monkeypatch.setattr(notes_sync, "repo_lock", lambda c, wait: waits.append(wait) or real_lock(c, wait))
    with real_lock(pair.a / ".git", 1):
        r = notes_sync.pull(pair.harness)
    assert r.status == "locked" and len(waits) == 1 and waits[0] <= 0.3 < notes_sync.LOCK_WAIT_S


def test_a_spent_budget_runs_no_command_but_cleanup_still_has_its_reserve(pair: Pair):
    repo = notes_sync.Repo(pair.a, 0)
    assert repo.git("status") is None
    r = repo.git("status", cleanup=True)
    assert r is not None and r.returncode == 0


# ------------------------------------------------------------------ review findings, 2026-10-02
# Each case below reproduced a defect the paired reviewer found in the first draft.


def test_cleanup_calls_share_one_reserve(pair: Pair, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(notes_sync.time, "monotonic", lambda: clock[0])
    timeouts: list[float] = []

    def fake_run(cmd, **kw):
        timeouts.append(kw["timeout"])
        clock[0] += 4.0
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notes_sync.procs, "run_bounded", fake_run)
    repo = notes_sync.Repo(pair.a, 0)
    repo.git("rebase", "--abort", cleanup=True)
    repo.git("rev-parse", "HEAD", cleanup=True)
    assert timeouts == [5.0, 1.0]


def test_an_edit_during_the_fetch_is_never_rebased_or_stashed(pair: Pair, monkeypatch):
    pair.commit(pair.a, "from-a.md", "a\n")
    pair.commit(pair.b, "from-b.md", "b\n", push=True)
    git(pair.a, "config", "rebase.autoStash", "true")
    head = git(pair.a, "rev-parse", "HEAD")
    real = notes_sync.Repo.git
    seen: list[tuple[str, ...]] = []

    def edit_during_fetch(self, *args, cap=notes_sync.LOCAL_CAP_S, cleanup=False):
        seen.append(args)
        r = real(self, *args, cap=cap, cleanup=cleanup)
        if args[0] == "fetch":
            (pair.a / "seed.md").write_text("concurrent edit\n", encoding="utf-8")
        return r

    monkeypatch.setattr(notes_sync.Repo, "git", edit_during_fetch)
    assert notes_sync.sync(pair.harness).status == "deferred"
    assert not any(a[0] == "rebase" for a in seen)
    assert git(pair.a, "rev-parse", "HEAD") == head
    assert (pair.a / "seed.md").read_text(encoding="utf-8") == "concurrent edit\n"


def test_a_clean_hi_turn_clears_a_failure_once_head_matches_upstream(pair: Pair, calls):
    url = git(pair.a, "remote", "get-url", "origin")
    git(pair.a, "remote", "set-url", "origin", str(pair.remote.parent / "gone.git"))
    assert notes_sync.pull(pair.harness).status == "offline"
    git(pair.a, "remote", "set-url", "origin", url)
    calls.clear()
    assert notes_sync.sync(pair.harness).status == "current"
    assert "failure" not in state(pair)
    assert "fetch" not in verbs(calls)


def test_a_hi_turn_behind_upstream_keeps_the_failure(pair: Pair):
    git(pair.a, "remote", "set-url", "origin", str(pair.remote.parent / "gone.git"))
    notes_sync.pull(pair.harness)
    pair.commit(pair.b, "b.md", "b\n", push=True)
    git(pair.a, "fetch", "-q", str(pair.remote), "main:refs/remotes/origin/main")
    assert notes_sync.sync(pair.harness).status == "current"
    assert state(pair)["failure"]["status"] == "offline"


@pytest.mark.parametrize("where", ["config", "marker"])
def test_undecodable_files_never_raise(pair: Pair, where: str):
    target = pair.harness / ("agent.local.yaml" if where == "config" else ".agent/notes-root")
    target.write_bytes(b"\xff\xfe")
    assert notes_sync.pull(pair.harness).status in ("off", "error")
    assert notes_sync.sync(pair.harness).status in ("off", "error")


def test_a_failed_status_read_never_pushes(pair: Pair, monkeypatch):
    pair.commit(pair.a, "from-a.md", "a\n")
    real = notes_sync.Repo.out
    reads = [0]

    def fail_second_status(self, *args, cleanup=False):
        if args == ("status", "--porcelain"):
            reads[0] += 1
            if reads[0] > 1:
                return None
        return real(self, *args, cleanup=cleanup)

    monkeypatch.setattr(notes_sync.Repo, "out", fail_second_status)
    before = git(pair.remote, "rev-parse", "main")
    assert notes_sync.sync(pair.harness).status == "error"
    assert git(pair.remote, "rev-parse", "main") == before


def test_an_untrustworthy_state_file_stops_every_mutation_and_is_kept(pair: Pair):
    path = pair.a / ".git" / "agent-sync" / "state.json"
    path.parent.mkdir()
    (pair.a / "new.md").write_text("new\n", encoding="utf-8")
    head = git(pair.a, "rev-parse", "HEAD")
    for text in ("{broken", "[]", '{"halt": "yes"}', '{"halt": {}}'):
        path.write_text(text, encoding="utf-8")
        for fn in (notes_sync.sync, notes_sync.pull):
            r = fn(pair.harness)
            assert r.status == "error" and "fix or remove it" in r.detail, text
        assert git(pair.a, "rev-parse", "HEAD") == head
        assert path.read_text(encoding="utf-8") == text
        standing = notes_sync.standing_failure(pair.harness)
        assert standing is not None and standing["id"] == "state-invalid", text
        with pytest.raises(notes_sync.core.AgentError, match="remove it by hand"):
            notes_sync.clear_halt(pair.harness)
