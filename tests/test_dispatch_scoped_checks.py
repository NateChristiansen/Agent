"""A dispatch attempt runs only the tests its diff selects, says so in its result, and acceptance
derives the selection again rather than trusting what was published."""

from __future__ import annotations

import shutil
from pathlib import Path

from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.state import load, save
from dispatch_helpers import _set_alpha_dispatch, droot  # noqa: F401 -- pytest fixture
from land_helpers import git as _git
from test_dispatch_verification import finish, ready  # noqa: F401 -- pytest fixtures

ROOT = Path(__file__).resolve().parents[1]


def _scoped(ready) -> tuple[Path, dict, Path]:
    root, d = ready
    wt = Path(d["worktree"])
    (wt / "tests").mkdir(exist_ok=True)
    shutil.copy(ROOT / "tests" / "selection.toml", wt / "tests" / "selection.toml")
    for name in ("test_dispatch_spend.py", "test_policy_corpus.py", "test_notes.py"):
        (wt / "tests" / name).write_text("")
    (wt / "src" / "agent_cli" / "dispatch").mkdir(parents=True)
    (wt / "src" / "agent_cli" / "dispatch" / "spend.py").write_text("x = 1\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "base with a selection table")
    d = load(root, d["id"])
    d["start_sha"] = _git(wt, "rev-parse", "HEAD")
    d["selection_policy"] = 2  # the base has a table now; the launch stamped the bare repo 0
    save(root, d)
    (wt / "src" / "agent_cli" / "dispatch" / "spend.py").write_text("x = 2\n")
    _git(wt, "commit", "-qam", "change spend")
    args = root / "pytest-args"
    _set_alpha_dispatch(
        root,
        verify=[
            {"command": "git diff --exit-code HEAD --", "timeout_seconds": 5},
            {"command": f"sh -c 'echo \"$@\" > {args.as_posix()}' pytest", "timeout_seconds": 5},
        ],
        verify_timeout_seconds=12,
    )
    return root, d, args


def test_a_scoped_attempt_runs_only_its_selected_tests_and_says_so(ready):
    root, d, args = _scoped(ready)
    gate.execute(root, d["id"], d["attempt"])
    assert args.read_text().split() == ["tests/test_dispatch_spend.py", "tests/test_policy_corpus.py"]
    after = load(root, d["id"])
    [scope] = after["verification_scopes"]
    assert scope["scope"] == "scoped" and scope["paths"] == ["src/agent_cli/dispatch/spend.py"]
    result = finish(root, after)["verification_result"]
    assert result["status"] == "verified" and result["reasons"][0].startswith("scoped check: 2 test files")


def test_acceptance_refuses_a_published_selection_that_no_longer_matches(ready):
    root, d, _ = _scoped(ready)
    gate.execute(root, d["id"], d["attempt"])
    forged = load(root, d["id"])
    forged["verification_scopes"][-1]["digest"] = "0" * 64
    save(root, forged)
    assert finish(root, load(root, d["id"]))["verification_result"]["status"] == "unverified"


def test_a_repo_without_a_table_runs_its_full_commands(ready):
    root, d = ready
    wt = Path(d["worktree"])
    d = load(root, d["id"])
    d["start_sha"] = _git(wt, "rev-parse", "HEAD")
    save(root, d)
    (wt / "README.md").write_text("changed\n")
    _git(wt, "commit", "-qam", "change")
    gate.execute(root, d["id"], d["attempt"])
    after = load(root, d["id"])
    assert after["verification_scopes"][-1]["reason"] == "no-table"
    assert finish(root, after)["verification_result"] == {"status": "verified", "reasons": []}
