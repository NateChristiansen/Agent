"""`pull-warn` and `push-guard`: the split of `src/agent_cli/hooks/repo_guards.py`, mirroring
the source split of the old hooks.py.
"""

import json
from pathlib import Path

import pytest

from conftest import git
from hooks_test_helpers import _ctx, _decision, _reason

pytestmark = pytest.mark.usefixtures("notes_remote")

# ---------------------------------------------------------------- pull-warn


def _read(path: str, tool: str = "Read", session_id: str = "s1") -> str:
    key = "file_path" if tool == "Read" else "path"
    return json.dumps(
        {
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {key: path},
        }
    )


def test_pull_warn_skips_registered_harness_checkout(tmp_root: Path, monkeypatch):
    from agent_cli import repos
    from agent_cli.hooks.repo_guards import pull_warn

    registry = repos.Registry(1, [repos.Repo("Harness", "agent", ".", "unused")])
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    assert pull_warn(tmp_root, json.loads(_read(str(tmp_root / "src/x.py")))) == ""


def test_pull_warn_asks_git_for_a_linked_harness_tree_when_discovery_cannot_say(tmp_path: Path, monkeypatch):
    """Windows has no `os.getuid`, so `git_discovery.common_dir` is always None there."""
    from agent_cli import git_discovery, repos
    from agent_cli.hooks.repo_guards import pull_warn

    harness = tmp_path / "manager"
    harness.mkdir()
    git(["init", "-b", "main"], harness)
    git(
        ["-c", "user.name=F", "-c", "user.email=f@example.invalid", "commit", "--allow-empty", "-m", "base"],
        harness,
    )
    linked = tmp_path / "linked"
    git(["worktree", "add", "-b", "side", str(linked), "main"], harness)
    registry = repos.Registry(1, [repos.Repo("Harness", "agent", str(harness), "unused")])
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    monkeypatch.setattr(git_discovery, "common_dir", lambda start: None)
    payload = json.loads(_read(str(harness / "README.md"), session_id="slow"))
    assert pull_warn(linked, payload) == ""


def test_pull_warn_runs_no_git_when_discovery_answers(tmp_path: Path, monkeypatch):
    from agent_cli import git_discovery, repos
    from agent_cli.hooks import repo_guards

    harness, linked = tmp_path / "manager", tmp_path / "linked"
    harness.mkdir()
    linked.mkdir()
    registry = repos.Registry(1, [repos.Repo("Harness", "agent", str(harness), "unused")])
    monkeypatch.setattr(repos, "load_registry", lambda root: registry)
    monkeypatch.setattr(git_discovery, "common_dir", lambda start: tmp_path / "common.git")

    def no_git(cwd, args):
        raise AssertionError(f"git {args} ran")

    monkeypatch.setattr(repo_guards, "_git_line", no_git)
    payload = json.loads(_read(str(harness / "README.md"), session_id="fast"))
    assert repo_guards.pull_warn(linked, payload) == ""


def test_pull_warn_fires_once_per_repo_per_session(tmp_root: Path, run):
    """Advisory, never a denial, and once per repo per session — a sweep of thirty files costs
    one line."""
    alpha = str(tmp_root / "repos/alpha-api/README.md")
    r = run(["hook", "pull-warn"], stdin=_read(alpha))
    ctx = _ctx(r)
    assert "alpha has not been pulled" in ctx
    assert "repos_sync" in ctx and "pull=True" in ctx
    assert "permissionDecision" not in r.out  # advisory, not a denial
    assert run(["hook", "pull-warn"], stdin=_read(alpha)).out == ""
    assert run(["hook", "pull-warn"], stdin=_read(alpha, tool="Grep")).out == ""


def test_pull_warn_is_silent_after_repos_sync_pull(tmp_root: Path, run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert run(["repos", "sync", "alpha", "--pull"]).code == 0
    alpha = str(tmp_root / "repos/alpha-api/README.md")
    assert run(["hook", "pull-warn"], stdin=_read(alpha)).out == ""


def test_pull_warn_ignores_paths_outside_a_registered_clone(tmp_root: Path, run):
    assert run(["hook", "pull-warn"], stdin=_read(str(tmp_root / "harness/README.md"))).out == ""
    assert run(["hook", "pull-warn"], stdin=_read("/etc/hosts")).out == ""
    assert run(["hook", "pull-warn"], stdin=_read("")).out == ""
    # no session id: nothing to record the warning against, so it stays quiet
    alpha = str(tmp_root / "repos/alpha-api/README.md")
    assert run(["hook", "pull-warn"], stdin=_read(alpha, session_id="")).out == ""


# ---------------------------------------------------------------- push-guard


def _record(tmp_root: Path, dispatch_id: str, **over) -> None:
    ddir = tmp_root / ".agent" / "dispatches"
    ddir.mkdir(parents=True, exist_ok=True)
    record = {"id": dispatch_id, "feature": 1, "branch": "agent/001-x", "state": "done", **over}
    (ddir / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


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


def _branch(repo: Path, name: str) -> str:
    import subprocess

    subprocess.run(["git", "checkout", "-q", "-b", name], cwd=repo, check=True)
    return subprocess.run(
        ["git", "log", "-1", "--format=%cI"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_push_guard_denies_a_dispatched_branch_with_no_review(tmp_root: Path, run):
    """A build worker's `done` is not a review, and since ADR-0063 neither is a finished review-row
    dispatch: the push waits for a clean `code-review` round on the pushed head."""
    run(["feature", "new", "x", "--title", "X"])
    repo = tmp_root / "repos/alpha-api"
    _branch(repo, "agent/001-x")
    _record(tmp_root, "001-01", choice_reason="build row: a page with tests")
    _record(
        tmp_root, "001-02", choice_reason=REVIEW, finished="2099-01-01T00:00:00-07:00", start_sha=_head(repo)
    )
    r = run(["hook", "push-guard"], stdin=_push("git push origin agent/001-x", repo))
    assert _decision(r) == "deny"
    reason = _reason(r)
    assert "no clean code-review round" in reason and "`code-review`" in reason
    assert "AGENT_PUSH_UNREVIEWED=1" in reason
    _clean_round(tmp_root, _head(repo))
    assert run(["hook", "push-guard"], stdin=_push("git push origin agent/001-x", repo)).out == ""


def test_push_guard_denies_git_c_from_vault_root(tmp_root: Path, run):
    """'review comments' in a mechanical reason is not a review dispatch."""
    run(["feature", "new", "x", "--title", "X"])
    repo = tmp_root / "repos/alpha-api"
    _branch(repo, "agent/001-x")
    _record(
        tmp_root,
        "001-01",
        choice_reason="mechanical: review comments name the change (delete comments); shape known",
        finished="2099-01-01T00:00:00-07:00",
    )
    command = f"git -C {repo} push origin agent/001-x"
    assert _decision(run(["hook", "push-guard"], stdin=_push(command, tmp_root))) == "deny"


def test_push_guard_catches_a_renamed_branch_by_the_worktree(tmp_root: Path, run):
    """Found live 2026-09-17 (reported by the 023 manager): the guard matched the
    branch NAME only. A worker's commits pushed as `feature/...` matched no record and sailed
    through unreviewed. A push from a dispatch's worktree is guarded whatever the branch is
    called, and a branch cut from the worker's is the same case."""
    run(["feature", "new", "x", "--title", "X"])
    repo = tmp_root / "repos/alpha-api"
    _branch(repo, "feature/renamed-by-hand")
    _record(tmp_root, "001-01", choice_reason="build row: a page with tests", worktree=str(repo))
    r = run(["hook", "push-guard"], stdin=_push("git push origin feature/renamed-by-hand", repo))
    assert _decision(r) == "deny"
    # a clean round on the head under the dispatch's branch name allows it
    _clean_round(tmp_root, _head(repo))
    assert run(["hook", "push-guard"], stdin=_push("git push origin feature/renamed-by-hand", repo)).out == ""
    # a push from somewhere else in the same repo tree is still guarded; the manager's own
    # vault root is not
    assert run(["hook", "push-guard"], stdin=_push("git push", tmp_root)).out == ""


def _head(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


REVIEW = "review row: read the branch"


def _clean_round(root: Path, head: str, branch: str = "agent/001-x") -> None:
    from agent_cli import code_review

    base = code_review.reviews_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    rec = {
        "repo": "alpha-api",
        "branch": branch,
        "head": head,
        "round": 1,
        "phase": "complete",
        "blocking": [],
    }
    (base / f"{head}-1.json").write_text(json.dumps(rec), encoding="utf-8")


def test_push_guard_ignores_branches_no_dispatch_ever_touched(tmp_root: Path, run):
    repo = tmp_root / "repos/alpha-api"
    _branch(repo, "harness/016-t11")
    assert run(["hook", "push-guard"], stdin=_push("git push origin harness/016-t11", repo)).out == ""
    # a push with no refspec cannot be listed, so in a registered repo it is refused anyway
    assert _decision(run(["hook", "push-guard"], stdin=_push("git push", repo))) == "deny"
    # and it is a git-push guard, not a substring guard
    assert run(["hook", "push-guard"], stdin=_push("git log --grep push", repo)).out == ""
    assert run(["hook", "push-guard"], stdin=_push("grep -r push .", repo)).out == ""


def test_push_guard_override_is_honoured_and_logged(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    repo = tmp_root / "repos/alpha-api"
    _branch(repo, "agent/001-x")
    _record(tmp_root, "001-01", choice_reason="build row: a page with tests")
    cmd = "AGENT_PUSH_UNREVIEWED=1 git push origin agent/001-x"
    assert run(["hook", "push-guard"], stdin=_push(cmd, repo)).out == ""
    log = (tmp_root / "features/001-x/log.md").read_text()
    assert "AGENT_PUSH_UNREVIEWED=1" in log
    assert "[!note]" in log
