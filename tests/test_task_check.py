"""`task_check`: read-only evidence that open tasks may be done or stale.

The fixture replays a feature's pickup in miniature: a plan written on 10-02, a product repo that moved
under it afterwards (a path rewritten, a path deleted, a commit naming the task), another
feature's tick that took a task over, and a task added after the change that touched its path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_cli import features, task_check

PLAN = """---
id: 1
slug: launch
title: Launch
status: active
opened: 2026-10-02
repos: []
---

# 001 launch

## Context
Ship the launch.

## Tasks
- [x] Write the parser
- [ ] Fix the KeyError in `src/pkg/terminal.py` on cancel
- [ ] Teach doctor about plan mode in `pkg/wiring.py`
- [ ] Write the user docs
- [ ] Make the stop group faster (groups.py)
- [-] Old idea (superseded: dropped; 001 #2)
- [ ] Port the exporter
"""

LATER_TASK = "- [ ] Retry the cancel in terminal.py after the rewrite\n"

OTHER = """---
id: 2
slug: other
title: Other
status: active
opened: 2026-10-02
---

## Tasks
- [x] Port the exporter (moved from 001 task 7; done in abc1234)
"""


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    env = {**os.environ}
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def _init(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    return path


def _commit(repo: Path, message: str, when: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, when=when)


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    product = _init(tmp_path / "product")
    for name in ("terminal.py", "wiring.py", "groups.py", "docs.py"):
        (product / "src" / "pkg").mkdir(parents=True, exist_ok=True)
        (product / "src" / "pkg" / name).write_text("x = 1\n", encoding="utf-8")
    _commit(product, "start", "2026-10-01T10:00:00+00:00")

    notes = _init(tmp_path / "notes")
    plan = notes / "features" / "001-launch" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(PLAN, encoding="utf-8")
    other = notes / "features" / "002-other" / "plan.md"
    other.parent.mkdir(parents=True)
    other.write_text(OTHER, encoding="utf-8")
    _commit(notes, "plans", "2026-10-02T10:00:00+00:00")

    (product / "src" / "pkg" / "terminal.py").write_text("x = 2\n", encoding="utf-8")
    _commit(product, "terminal: finalize returns a result", "2026-10-04T10:00:00+00:00")
    (product / "src" / "pkg" / "wiring.py").unlink()
    _commit(product, "remove the wiring checks", "2026-10-04T11:00:00+00:00")
    _commit_empty = ["commit", "-q", "--allow-empty", "-m", "001 #5: the stop group skips idle members"]
    _git(product, *_commit_empty, when="2026-10-04T12:00:00+00:00")

    plan.write_text(PLAN + LATER_TASK, encoding="utf-8")
    _commit(notes, "a later task", "2026-10-06T10:00:00+00:00")
    return notes, product


def _check(world: tuple[Path, Path]) -> dict:
    notes, product = world
    f = features.resolve(notes, "1")
    return task_check.check(notes, f, repos=[task_check.RepoRef("product", product, "main")])


def test_each_open_task_gets_its_evidence(world: tuple[Path, Path]):
    result = _check(world)
    by = {t["index"]: t for t in result["tasks"]}
    assert sorted(by) == [2, 3, 4, 5, 7, 8]  # done #1 and superseded #6 are not open
    assert by[2]["verdict"] == "changed under it"
    assert by[2]["added"]["date"].startswith("2026-10-02")
    assert "src/pkg/terminal.py: 1 commit(s) since added" in by[2]["changed"][0]
    assert by[3]["verdict"] == "changed under it"
    assert "pkg/wiring.py is gone from main" in by[3]["changed"][0]
    assert by[4]["verdict"] == "no signal"
    assert by[5]["verdict"] == "possibly done"
    assert "001 #5: the stop group skips idle members" in by[5]["done_signals"][0]
    assert by[7]["verdict"] == "possibly done"
    assert by[7]["done_signals"][0].startswith("002 #1 done")
    # Added on 10-06, after the 10-04 change to terminal.py: born from `git log -S`, not the
    # plan's opened date, so the older commit is no signal against it.
    assert by[8]["added"]["date"].startswith("2026-10-06")
    assert by[8]["verdict"] == "no signal"
    assert result["flagged"] == 4
    assert result["context"]["edited"].startswith("2026-10-02")
    assert result["context"]["commits_since"] == {"product": 3}


def test_the_text_is_short_and_says_what_to_do(world: tuple[Path, Path]):
    text = task_check.render(_check(world))
    lines = text.splitlines()
    assert lines[0].startswith("task check 001 launch: 6 open, 4 flagged (read-only):")
    assert "task_supersede" in lines[0]
    assert lines[1].startswith("  Context last edited 2026-10-02")
    assert "product has 3 commit(s) since" in lines[1]
    assert any(line.startswith("  #2 changed under it (added 2026-10-02)") for line in lines)
    assert lines[-1] == "  no signal: #4, #8"
    assert all(len(line) <= task_check.MAX_LINE for line in lines)


def test_it_never_edits(world: tuple[Path, Path]):
    notes, _ = world
    before = {p: p.read_bytes() for p in notes.rglob("*.md")}
    _check(world)
    assert {p: p.read_bytes() for p in notes.rglob("*.md")} == before
    assert _git(notes, "status", "--porcelain") == ""


def test_a_feature_with_no_open_tasks_says_so_in_one_line(world: tuple[Path, Path]):
    notes, _ = world
    f = features.resolve(notes, "2")
    result = task_check.check(notes, f, repos=[])
    assert task_check.render(result) == "task check 002 other: 0 open"


def test_cited_symbols_and_landed_branches_are_signals(world: tuple[Path, Path]):
    """One task named `terminal.finalize`, not a file, and another named the branch
    `fix/cleanup-2026-10-11` that a "Land cleanup (fix/cleanup-…)" commit then merged."""
    notes, product = world
    plan = notes / "features" / "003-third" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\nid: 3\nslug: third\ntitle: Third\nstatus: active\nopened: 2026-10-02\n---\n\n## Tasks\n"
        "- [ ] Decide the `pkg.finalize_result` contract\n"
        "- [ ] `fix/topic9-x` lands on main\n"
        "- [ ] Keep `untouched_name` as is\n",
        encoding="utf-8",
    )
    _commit(notes, "third", "2026-10-02T12:00:00+00:00")
    (product / "src" / "pkg" / "docs.py").write_text("def finalize():\n    return pkg.finalize_result\n")
    _commit(product, "docs: return the result", "2026-10-05T10:00:00+00:00")
    _git(
        product, "commit", "-q", "--allow-empty", "-m", "Land fix/topic9-x", when="2026-10-05T11:00:00+00:00"
    )
    f = features.resolve(notes, "3")
    result = task_check.check(notes, f, repos=[task_check.RepoRef("product", product, "main")])
    by = {t["index"]: t for t in result["tasks"]}
    assert by[1]["verdict"] == "changed under it"
    assert by[1]["changed"][0].startswith("`pkg.finalize_result`: 1 commit(s) since added (")
    assert by[2]["verdict"] == "possibly done"
    assert "Land fix/topic9-x" in by[2]["done_signals"][0]
    assert by[3]["verdict"] == "no signal"


def test_task_symbols():
    text = "the `terminal.finalize` contract, `CODEX_ABSENT_HOOKS` in `doctor/checks.py`, `plan-mode-allow`, `ok`"
    assert task_check.task_symbols(text) == ["terminal.finalize", "CODEX_ABSENT_HOOKS", "plan-mode-allow"]


def _feature_with_alpha(tmp_root: Path, run) -> None:
    r = run(["feature", "new", "x", "--title", "X", "--repo", "alpha"])
    assert r.code == 0, r.err
    run(["task", "add", "1", "do the thing in README.md"])
    contract = tmp_root / "harness" / "worker-contract.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("# worker contract (test stub)\n")


@pytest.mark.usefixtures("notes_remote")
def test_the_first_dispatch_of_a_session_carries_the_check(tmp_root: Path, run, fake_claude):
    from agent_cli import dispatch as dispatch_mod
    from dispatch_helpers import envelope, one

    _feature_with_alpha(tmp_root, run)
    # C2 binds launch choices too: preview the same exemption used at launch below.
    preview = [
        "dispatch",
        "new",
        "alpha",
        "--feature",
        "1",
        "--prompt",
        "x",
        "--no-checks",
        "--dry-run",
        "--session-id",
    ]
    r = run(["--json", *preview, "s1"])
    assert r.code == 0, r.err
    assert r.json["task_check"]["open"] == 1
    assert "\n  task check 001 x: 1 open" in run([*preview, "s1"]).out
    fake_claude.scenario([one(envelope())])
    dispatch_mod.new(
        tmp_root, repo="alpha", feature="1", prompt="x", watch=True, no_checks=True, session_id="s1"
    )
    r = run(["--json", *preview, "s1"])
    assert r.json["task_check"] is None
    assert "task check" not in run([*preview, "s1"]).out
    assert run(["--json", *preview, "s2"]).json["task_check"]["open"] == 1


@pytest.mark.usefixtures("notes_remote")
def test_the_preview_warns_about_a_directory_claim(tmp_root: Path, run, fake_claude):
    _feature_with_alpha(tmp_root, run)
    alpha = tmp_root / "repos" / "alpha-api"
    (alpha / "tests").mkdir()
    (alpha / "lib").mkdir()
    for n in range(21):
        (alpha / "lib" / f"m{n}.py").write_text("x = 1\n", encoding="utf-8")
    (alpha / "tests" / "test_a.py").write_text("x = 1\n", encoding="utf-8")
    _commit(alpha, "files", "2026-10-01T10:00:00+00:00")
    base = ["dispatch", "new", "alpha", "--feature", "1", "--prompt", "x", "--dry-run"]
    r = run(["--json", *base, "--writes", "lib", "--writes", "tests", "--writes", "README.md"])
    assert r.code == 0, r.err
    assert [w.split(":")[0] for w in r.json["writes_warnings"]] == [
        "writes `lib` is a directory with 21 tracked files",
        "writes `tests` is a directory with 1 tracked files",
    ]
    r = run([*base, "--writes", "lib/m1.py", "--writes", "README.md"])
    assert "warning: writes" not in r.out
    assert "warning: writes `lib` is a directory" in run([*base, "--writes", "lib"]).out


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        (
            "Fix `src/agent_cli/hooks/groups.py` and terminal.py:65.",
            ["src/agent_cli/hooks/groups.py", "terminal.py"],
        ),
        ("see [[features/005-x/plan|005]] and https://example.com/a/b.py", []),
        ("Mac/Windows × Claude/Codex", ["Mac/Windows", "Claude/Codex"]),
        ("e.g. the docs, v1.2 and setup.py", ["setup.py"]),
    ],
)
def test_task_paths(text: str, paths: list[str]):
    assert task_check.task_paths(text) == paths


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("005 #6 landed", True),
        ("from 005 task 6", True),
        ("005-example #6", True),
        ("005 T6", True),
        ("005 #61", False),
        ("1005 #6", False),
        ("006 T6", False),
    ],
)
def test_task_mention(text: str, hit: bool):
    assert bool(task_check.task_mention_re(5, 6).search(text)) is hit
