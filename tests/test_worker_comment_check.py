"""A dispatch worker's commit that would add comment noise is refused at commit time."""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from agent_cli.dispatch.codex_permissions import decision, git_metadata_refusal
from agent_cli.dispatch.rules import sdk_hooks
from agent_cli.hooks import worker_comments
from agent_cli.hooks.paths import PROTECTED_NOTE
from agent_cli.hooks.worker_comments import comment_noise, noisy, takes_worktree
from dispatch_helpers import droot  # noqa: F401 -- pytest fixture

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "git commit -m 'Add helpers'"


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    wt = tmp_path / "worktree"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "T")
    (wt / "README.md").write_text("readme\n", encoding="utf-8")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-q", "-m", "init")
    return wt, _git(wt, "rev-parse", "HEAD").strip()


def _stage(wt: Path, name: str, body: str) -> None:
    (wt / name).write_text(body, encoding="utf-8")
    _git(wt, "add", name)


def _claude(wt: Path, start: str | None, command: str = COMMIT) -> dict[str, Any]:
    callback = sdk_hooks(wt, wt, start)["PreToolUse"][0].hooks[0]
    payload: Any = {"tool_name": "Bash", "tool_input": {"command": command}}
    return cast(dict[str, Any], asyncio.run(callback(payload, None, {"signal": None})))


def _codex(root: Path, wt: Path, start: str | None, monkeypatch, command: str = COMMIT) -> tuple[str, str]:
    from agent_cli.dispatch import codex_permissions

    record = {"id": "eval", "repo": "alpha", "worktree": str(wt), "start_sha": start}
    monkeypatch.setattr(codex_permissions.state, "load", lambda *_args: record)
    return decision(root, "eval", {"tool_name": "Bash", "tool_input": {"command": command}})


def _denied_by_check(claude: dict[str, Any], codex: tuple[str, str]) -> tuple[str, str]:
    out = claude["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert codex[0] == "deny"
    return out["permissionDecisionReason"], codex[1]


def test_both_providers_deny_a_comment_citing_a_feature_id(droot: Path, repo, monkeypatch):
    wt, start = repo
    _stage(wt, "helpers.py", "def f():\n    # Added for feature 028 so the gate passes.\n    return 1\n")

    reasons = _denied_by_check(_claude(wt, start), _codex(droot, wt, start, monkeypatch))

    for reason in reasons:
        assert 'helpers.py:2 cites "feature 028"' in reason
        assert "Delete each comment" in reason
        assert PROTECTED_NOTE in reason


@pytest.mark.parametrize(
    "cited", ["feature 028", "Feature 028", "001-mac-01", "001-mac-01.", "L-20261004-17"]
)
def test_each_kind_of_id_is_refused(cited: str):
    rows = noisy([{"path": "a.py", "line": 1, "end": 1, "text": f"See {cited} for why."}])
    assert len(rows) == 1


@pytest.mark.parametrize(
    "text",
    [
        "codes 100-10 are informational",
        "ports 443-80 both",
        "task 1 of the pipeline runs",
        "dispatch 5 workers",
        "needs features 2 and 3",
    ],
)
def test_prose_with_numbers_is_allowed_on_both(text: str, droot: Path, repo, monkeypatch):
    wt, start = repo
    _stage(wt, "notes.py", f"# {text}\nx = 1\n")

    assert _claude(wt, start) == {}
    verdict, reason = _codex(droot, wt, start, monkeypatch)
    assert verdict == "allow", reason


@pytest.mark.parametrize("text", ["feature 028", "per 001-mac-01", "L-20261004-17"])
def test_cited_ids_are_denied_on_both(text: str, droot: Path, repo, monkeypatch):
    wt, start = repo
    _stage(wt, "notes.py", f"# {text}\nx = 1\n")

    _denied_by_check(_claude(wt, start), _codex(droot, wt, start, monkeypatch))


def test_a_failed_history_log_is_denied_on_both(droot: Path, repo, monkeypatch):
    wt, start = repo
    _stage(wt, "notes.py", "x = 1\n")
    real = worker_comments.repos_mod.git_or_none

    def fail_log(args, *rest, **kw):
        return None if args[0] == "log" else real(args, *rest, **kw)

    monkeypatch.setattr(worker_comments.repos_mod, "git_or_none", fail_log)

    claude = _claude(wt, start)
    assert claude["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _codex(droot, wt, start, monkeypatch)[0] == "deny"


def test_repeat_across_three_one_file_commits_is_denied_on_the_third(droot: Path, repo, monkeypatch):
    wt, start = repo
    boiler = "# Shared fixtures live in conftest.\nimport os\n"
    for name in ("test_a.py", "test_b.py"):
        _stage(wt, name, boiler)
        assert _claude(wt, start) == {}
        assert _codex(droot, wt, start, monkeypatch)[1].startswith("This commit") is False
        _git(wt, "commit", "-q", "-m", name)
    _stage(wt, "test_c.py", boiler)

    reasons = _denied_by_check(_claude(wt, start), _codex(droot, wt, start, monkeypatch))

    for reason in reasons:
        for name in ("test_a.py", "test_b.py", "test_c.py"):
            assert f"{name}:1 same text in 3 files" in reason


def test_commit_all_reads_unstaged_tracked_changes(droot: Path, repo, monkeypatch):
    wt, start = repo
    _stage(wt, "helpers.py", "x = 1\n")
    _git(wt, "commit", "-q", "-m", "helpers")
    (wt / "helpers.py").write_text("# Per L-20261005-13.\nx = 1\n", encoding="utf-8")

    assert _claude(wt, start) == {}
    reasons = _denied_by_check(
        _claude(wt, start, "git commit -am 'x'"), _codex(droot, wt, start, monkeypatch, "git commit -am 'x'")
    )
    assert all("helpers.py:1" in r for r in reasons)


def test_one_justified_comment_is_allowed_on_both(droot: Path, repo, monkeypatch):
    wt, start = repo
    _stage(wt, "client.py", "# The API drops the first call after idle, so retry once.\nRETRIES = 1\n")
    _stage(wt, "test_client.py", "def test_x():\n    # Given a client\n    pass\n")

    assert _claude(wt, start) == {}
    verdict, reason = _codex(droot, wt, start, monkeypatch)
    assert verdict == "allow", reason


def test_given_when_then_and_directives_are_exempt_from_the_repeat():
    comments = [
        {"path": f"test_{i}.py", "line": n, "end": n, "text": text}
        for i in range(3)
        for n, text in enumerate(["Given a user", "When they log in", "Then it works", "fmt: off"], 1)
    ]
    assert noisy(comments) == []


def test_codex_git_metadata_approval_refuses_a_noisy_commit(droot: Path, repo, monkeypatch):
    from agent_cli.dispatch import codex_permissions

    wt, start = repo
    record = {"id": "eval", "repo": "alpha", "worktree": str(wt), "start_sha": start}
    monkeypatch.setattr(codex_permissions.state, "load", lambda *_args: record)
    params = {"kind": "command", "cwd": str(wt), "command": COMMIT}
    _stage(wt, "client.py", "# Retry once: the API drops the first call after idle.\nRETRIES = 1\n")
    assert git_metadata_refusal(droot, "eval", params) == ""
    _stage(wt, "helpers.py", "# From dispatch 002-mac-01.\nx = 1\n")
    assert git_metadata_refusal(droot, "eval", params)


def test_not_wired_for_the_manager():
    for config in (ROOT / ".claude/settings.json", ROOT / ".codex/hooks.json"):
        if config.is_file():
            text = config.read_text(encoding="utf-8")
            json.loads(text)
            assert "worker_comments" not in text
            assert "comment_noise" not in text
    importers = sorted(
        p.relative_to(ROOT / "src").as_posix()
        for p in (ROOT / "src").rglob("*.py")
        if "worker_comments" in p.read_text(encoding="utf-8") and p.name != "worker_comments.py"
    )
    assert importers == ["agent_cli/dispatch/codex_permissions.py", "agent_cli/dispatch/rules.py"]
    assert "PreToolUse" not in sdk_hooks(ROOT)


def test_a_broken_check_denies_the_commit(droot: Path, repo, monkeypatch):
    wt, start = repo

    def boom(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_comments, "committing_comments", boom)
    assert _claude(wt, start)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _codex(droot, wt, start, monkeypatch) == ("deny", "Worker comment check failed: RuntimeError")


def test_a_git_failure_raises(tmp_path: Path):
    with pytest.raises(RuntimeError):
        comment_noise(COMMIT, tmp_path, "deadbeef")


def test_other_commands_never_read_git(tmp_path: Path):
    assert comment_noise("git status", tmp_path, "deadbeef") is None
    assert comment_noise("git log --oneline", tmp_path, "deadbeef") is None


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-m", "msg"], False),
        (["-am", "msg"], True),
        (["--all", "-m", "msg"], True),
        (["-m", "msg", "src/a.py"], True),
        (["-uno", "-m", "msg"], False),
        (["--amend", "--no-edit"], False),
        (["-m", "-a"], False),
    ],
)
def test_which_commits_take_the_working_tree(args: list[str], expected: bool):
    assert takes_worktree(args) is expected


def test_both_providers_deny_a_docstring_citing_an_id(droot: Path, repo, monkeypatch):
    """Docstrings escaped the line scanner; they are found with ast now (2026-10-05)."""
    wt, start = repo
    _stage(wt, "helpers.py", 'def f():\n    """Caps the spend (lesson L-20261005-13)."""\n    return 1\n')
    reasons = _denied_by_check(_claude(wt, start), _codex(droot, wt, start, monkeypatch))
    for reason in reasons:
        assert 'cites "L-20261005-13"' in reason


def test_a_repeated_docstring_across_three_separate_commits_is_refused(repo):
    wt, start = repo
    for name in ("a.py", "b.py"):
        _stage(wt, name, '"""Shared module boilerplate."""\nx = 1\n')
        _git(wt, "commit", "-q", "-m", name)
    _stage(wt, "c.py", '"""Shared module boilerplate."""\nx = 1\n')
    reason = comment_noise(COMMIT, wt, start)
    assert reason is not None and "same text in 3 files" in reason


def test_a_plain_string_and_a_useful_docstring_pass(repo):
    wt, start = repo
    _stage(
        wt,
        "m.py",
        'def f():\n    """Return one, the identity for multiplication."""\n    s = "feature 028"\n    return 1\n',
    )
    assert comment_noise(COMMIT, wt, start) is None


def _pending_merge(wt: Path) -> None:
    _git(wt, "checkout", "-q", "-b", "work")
    _stage(wt, "mine.py", "x = 1\n")
    _git(wt, "commit", "-q", "-m", "mine")
    _git(wt, "checkout", "-q", "main")
    _stage(wt, "theirs.py", "def f():\n    # Added for feature 028 so the gate passes.\n    return 1\n")
    _git(wt, "commit", "-q", "-m", "theirs")
    _git(wt, "checkout", "-q", "work")
    _git(wt, "merge", "-q", "--no-commit", "--no-ff", "main")


def test_a_merge_does_not_blame_lines_the_other_side_brought(repo):
    wt, start = repo
    _pending_merge(wt)
    assert comment_noise("git commit --no-edit", wt, start) is None


def test_a_comment_written_while_resolving_a_merge_is_still_refused(repo):
    wt, start = repo
    _pending_merge(wt)
    _stage(wt, "mine.py", "x = 1\n# Added for feature 029 to resolve the merge.\n")
    reason = comment_noise("git commit --no-edit", wt, start)
    assert reason is not None and "mine.py" in reason and "theirs.py" not in reason
