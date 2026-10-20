"""Regressions for `task_check`: it runs before the worker starts, its
budget bounds git and the Python scans alike, and a task's birth follows the line's own history,
whatever notes or qualifiers the line carries."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_cli import features, task_check
from agent_cli.ops.features import _pickup_check  # pyright: ignore[reportPrivateUsage]
from test_task_check import _commit, _feature_with_alpha, _git, _init
from test_tasks import _seed

posix_only = pytest.mark.skipif(os.name == "nt", reason="a shell-script git stand-in")


@pytest.mark.usefixtures("notes_remote")
def test_the_launch_check_runs_before_the_worker(tmp_root: Path, run, fake_claude, monkeypatch):
    from agent_cli.ops import dispatch as dispatch_ops
    from dispatch_helpers import envelope, one

    _feature_with_alpha(tmp_root, run)
    fake_claude.scenario([one(envelope())])
    seen: list[list[str]] = []
    real_check = task_check.check

    def checked(root: Path, feature: features.Feature, *args, **kwargs):
        seen.append(
            [d["state"] for d in dispatch_ops.dispatch_mod.load_all(root) if d["feature"] == feature.id]
        )
        return real_check(root, feature, *args, **kwargs)

    monkeypatch.setattr(task_check, "check", checked)
    result = dispatch_ops.dispatch_new(
        tmp_root, repo="alpha", feature="1", prompt="x", watch=True, no_checks=True, session_id="s1"
    )
    assert seen == [[]]  # no dispatch existed yet: the check ran before the launch
    assert result.data["task_check"]["open"] == 1
    assert "task check 001 x: 1 open" in result.text
    record = dispatch_ops.dispatch_mod.load(tmp_root, result.data["id"])
    flagged = [t["index"] for t in result.data["task_check"]["tasks"] if t["verdict"] != "no signal"]
    assert record["task_check"] == {"open": 1, "flagged": flagged, "incomplete": None}


def test_an_escaping_timeout_never_fails_pickup(tmp_path: Path):
    f = features.Feature(1, "x", tmp_path, {"opened": "2026-10-01"}, "## Tasks\n- [ ] pending\n")
    with patch("agent_cli.task_check.check", side_effect=subprocess.TimeoutExpired("git", 1)):
        checked, text = _pickup_check(tmp_path, f)
    assert checked is None and text.startswith("\ntask check skipped:")


def _slow_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `git` first on PATH that hangs, as a stuck credential helper or filter would."""
    bindir = tmp_path / "slowbin"
    bindir.mkdir()
    git = bindir / "git"
    git.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


@posix_only
@pytest.mark.usefixtures("notes_remote")
def test_a_hung_git_ends_in_an_incomplete_result_within_the_budget_and_feature_use_survives_it(
    tmp_root: Path, run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed(tmp_root, run)
    f = features.resolve(tmp_root, "1")
    _slow_git(tmp_path, monkeypatch)
    started = time.monotonic()
    result = task_check.check(tmp_root, f, repos=[], budget_s=0.3)
    assert time.monotonic() - started < 5
    assert result["incomplete"].startswith("timed out after 0.3 s")
    assert result["open"] == 2
    assert "incomplete: timed out after 0.3 s" in task_check.render(result)

    monkeypatch.setattr(task_check, "BUDGET_S", 0.3)
    started = time.monotonic()
    r = run(["feature", "use", "1", "--session-id", "s1"])
    assert time.monotonic() - started < 10
    assert r.code == 0, r.err
    assert "incomplete: timed out" in r.out


def test_a_repo_textconv_never_runs_inside_the_check(tmp_path: Path):
    """A repo's own diff driver held `_history` past an external cutoff (textconv sleeping 3 s)."""
    product = _init(tmp_path / "product")
    (product / ".gitattributes").write_text("*.py diff=p4review\n")
    (product / "a.py").write_text("old_symbol = 1\n")
    _commit(product, "old", "2026-10-02T10:00:00+00:00")
    (product / "a.py").write_text("old_symbol = 2\n")
    _commit(product, "changed", "2026-10-03T10:00:00+00:00")
    slow = tmp_path / "textconv.py"
    slow.write_text("import time\ntime.sleep(3)\n")
    _git(product, "config", "diff.p4review.textconv", f"{sys.executable} {slow}")
    started = time.monotonic()
    commits = task_check._history(  # pyright: ignore[reportPrivateUsage]
        task_check.RepoRef("p", product, "main"), "2026-10-01", True
    )
    assert time.monotonic() - started < 2.5
    assert any("old_symbol = 2" in c.changed for c in commits)


def test_a_tick_note_never_resets_a_task_birth(tmp_path: Path):
    """Added 10-02, its file rewritten 10-03, ticked with a note 10-04, unticked 10-06: the
    birth stays 10-02 and the 10-03 rewrite is a signal."""
    notes = _init(tmp_path / "notes")
    plan = notes / "features" / "001-x" / "plan.md"
    plan.parent.mkdir(parents=True)
    body = "## Tasks\n- [ ] Fix a.py ^t1\n"
    plan.write_text(body)
    _commit(notes, "added", "2026-10-02T10:00:00+00:00")
    product = _init(tmp_path / "product")
    (product / "a.py").write_text("x = 1\n")
    _commit(product, "start", "2026-10-01T10:00:00+00:00")
    (product / "a.py").write_text("x = 2\n")
    _commit(product, "rewrite", "2026-10-03T10:00:00+00:00")
    body = features.set_task(body, features.list_tasks(body)[0], True, "checked")
    plan.write_text(body)
    _commit(notes, "tick", "2026-10-04T10:00:00+00:00")
    body = features.set_task(body, features.list_tasks(body)[0], False)
    plan.write_text(body)
    _commit(notes, "undo", "2026-10-06T10:00:00+00:00")
    text = features.list_tasks(body)[0].text
    assert text == "Fix a.py (checked) ^t1"
    birth = task_check.added_at(plan, text)
    assert birth is not None and birth[1].startswith("2026-10-02")
    f = features.Feature(1, "x", plan.parent, {"opened": "2026-10-01"}, body)
    result = task_check.check(notes, f, repos=[task_check.RepoRef("p", product, "main")])
    assert result["tasks"][0]["verdict"] == "changed under it"
    assert "a.py: 1 commit(s) since added" in result["tasks"][0]["changed"][0]


def _plan_repo(tmp_path: Path) -> tuple[Path, Path]:
    notes = _init(tmp_path / "notes")
    plan = notes / "features" / "001-x" / "plan.md"
    plan.parent.mkdir(parents=True)
    return notes, plan


@pytest.mark.parametrize("note", ["verified (partial", "checked partial)"])
def test_a_note_with_an_unmatched_parenthesis_never_resets_birth(tmp_path: Path, note: str):
    """The text normaliser could not strip these notes, so the unticked line was
    dated to its tick."""
    notes, plan = _plan_repo(tmp_path)
    body = "## Tasks\n- [ ] Fix a.py\n"
    plan.write_text(body)
    _commit(notes, "added", "2026-10-02T10:00:00+00:00")
    body = features.set_task(body, features.list_tasks(body)[0], True, note)
    plan.write_text(body)
    _commit(notes, "tick", "2026-10-04T10:00:00+00:00")
    body = features.set_task(body, features.list_tasks(body)[0], False)
    plan.write_text(body)
    _commit(notes, "undo", "2026-10-06T10:00:00+00:00")
    birth = task_check.added_at(plan, features.list_tasks(body)[0].text)
    assert birth is not None and birth[1].startswith("2026-10-02"), birth


def test_a_qualifier_is_part_of_the_task_and_not_an_alias(tmp_path: Path):
    """ "Fix a.py (macOS)", added 10-06, was dated to "Fix a.py (Windows)" of 10-02."""
    notes, plan = _plan_repo(tmp_path)
    body = "## Tasks\n- [x] Fix a.py (Windows)\n"
    plan.write_text(body)
    _commit(notes, "windows task", "2026-10-02T10:00:00+00:00")
    body += "- [ ] Fix a.py (macOS)\n"
    plan.write_text(body)
    _commit(notes, "mac task", "2026-10-06T10:00:00+00:00")
    birth = task_check.added_at(plan, features.list_tasks(body)[1].text)
    assert birth is not None and birth[1].startswith("2026-10-06"), birth


def test_two_tasks_with_the_same_text_each_get_their_own_birth(tmp_path: Path):
    notes, plan = _plan_repo(tmp_path)
    plan.write_text("## Tasks\n- [ ] Ship it\n")
    _commit(notes, "one", "2026-10-02T10:00:00+00:00")
    plan.write_text("## Tasks\n- [ ] Ship it\n- [ ] Ship it\n")
    _commit(notes, "two", "2026-10-05T10:00:00+00:00")
    first, second = task_check.added_at(plan, "Ship it", 0), task_check.added_at(plan, "Ship it", 1)
    assert first and first[1].startswith("2026-10-02")
    assert second and second[1].startswith("2026-10-05")


def test_task_birth_does_not_run_an_external_diff(tmp_path: Path):
    notes, plan = _plan_repo(tmp_path)
    plan.write_text("## Tasks\n- [ ] Fix a.py\n")
    _commit(notes, "added", "2026-10-02T10:00:00+00:00")
    marker = notes / "driver-ran"
    driver = notes / "driver.py"
    driver.write_text("from pathlib import Path\nPath(__file__).with_name('driver-ran').touch()\n")
    _git(notes, "config", "diff.external", f"{sys.executable} {driver}")
    assert task_check.added_at(plan, "Fix a.py") is not None
    assert not marker.exists()


def test_an_uncommitted_task_gets_an_approximate_birth(tmp_path: Path):
    notes, plan = _plan_repo(tmp_path)
    plan.write_text("## Tasks\n- [ ] Fix a.py\n")
    _commit(notes, "added", "2026-10-02T10:00:00+00:00")
    body = "## Tasks\n- [ ] Fix a.py\n- [ ] Not committed yet\n"
    plan.write_text(body)
    assert task_check.added_at(plan, "Not committed yet") is None
    f = features.Feature(1, "x", plan.parent, {"opened": "2026-10-01"}, body)
    result = task_check.check(notes, f, repos=[])
    by = {t["index"]: t for t in result["tasks"]}
    assert by[1]["added"]["approximate"] is False and by[1]["added"]["date"].startswith("2026-10-02")
    assert by[2]["added"] == {"sha": None, "date": "2026-10-01", "approximate": True}


def test_the_budget_covers_evidence_reads_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The deadline was checked only around git calls."""
    f = features.Feature(1, "x", tmp_path, {"opened": "2026-10-01"}, "## Tasks\n- [ ] pending\n")
    monkeypatch.setattr(task_check, "_git", lambda *a: None)

    def slow_elsewhere(*args: object) -> list[tuple[str, str]]:
        time.sleep(0.2)
        return []

    monkeypatch.setattr(task_check, "_elsewhere", slow_elsewhere)
    result = task_check.check(tmp_path, f, repos=[], budget_s=0.01)
    assert result["incomplete"] is not None


def test_a_large_python_scan_stays_within_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """80 open tasks against 500 closed 20,000-character lines in another feature
    took 13.9 s on a 10 s budget and came back complete."""
    # Births are git reads, bounded elsewhere; without them the whole budget goes to the scan.
    monkeypatch.setattr(task_check, "added_at", lambda *args: None)
    root = tmp_path / "notes"
    current, other = root / "features/001-x", root / "features/002-other"
    current.mkdir(parents=True)
    other.mkdir(parents=True)
    body = "## Tasks\n" + "".join(f"- [ ] task {i}\n" for i in range(80))
    f = features.Feature(1, "x", current, {"status": "active", "opened": "2026-10-01"}, body)
    f.save()
    long_lines = ("- [x] " + "a1" * 10000 + "\n") * 60  # holds the feature number, so it is scanned
    features.Feature(2, "other", other, {"status": "active"}, "## Tasks\n" + long_lines).save()
    started = time.monotonic()
    result = task_check.check(root, f, repos=[], budget_s=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 0.2 + 1.5, elapsed
    assert result["incomplete"] is not None
    assert len(result["tasks"]) < 80


@pytest.mark.parametrize(
    "initial",
    [
        "## Notes\n- [ ] Fix a.py\n",
        "## Notes\n```markdown\n- [ ] Fix a.py\n```\n",
        "## Tasks\n- [ ] parent\n  - [ ] Fix a.py\n",
    ],
    ids=["outside-tasks", "fenced-example", "nested-sub-step"],
)
def test_birth_never_follows_a_same_text_checkbox_that_is_no_task(tmp_path: Path, initial: str):
    """A 10-02 checkbox with the task's text outside Tasks, in a fence or nested
    was taken as the task's line, so the 10-06 task was dated 10-02 and a 10-04 change flagged."""
    notes = _init(tmp_path / "notes")
    plan = notes / "features" / "001-work" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(initial, encoding="utf-8")
    _commit(notes, "day 2", "2026-10-02T10:00:00+00:00")
    (notes / "a.py").write_text("value = 1\n", encoding="utf-8")
    _commit(notes, "file before task", "2026-10-04T10:00:00+00:00")
    body = initial + "\n## Tasks\n- [ ] Fix a.py\n"
    plan.write_text(body, encoding="utf-8")
    _commit(notes, "day 6", "2026-10-06T10:00:00+00:00")
    task = features.list_tasks(body)[-1]
    birth = task_check.added_at(plan, task.text)
    assert birth is not None and birth[1].startswith("2026-10-06"), birth
    f = features.Feature(1, "work", plan.parent, {"opened": "2026-10-01"}, body)
    result = task_check.check(notes, f, repos=[task_check.RepoRef("p", notes, "main")])
    last = result["tasks"][-1]
    assert last["added"]["date"].startswith("2026-10-06") and last["added"]["approximate"] is False
    assert last["verdict"] == "no signal", last


def test_birth_counts_occurrences_in_frontmatter_plans(tmp_path: Path):
    """The committed file's frontmatter shifts line numbers; the task line is still found."""
    notes = _init(tmp_path / "notes")
    folder = notes / "features" / "001-x"
    folder.mkdir(parents=True)
    f = features.Feature(
        1, "x", folder, {"status": "active", "opened": "2026-10-01"}, "## Tasks\n- [ ] Ship it\n"
    )
    f.save()
    _commit(notes, "one", "2026-10-02T10:00:00+00:00")
    f.body += "- [ ] Ship it\n"
    f.save()
    _commit(notes, "two", "2026-10-05T10:00:00+00:00")
    births = [task_check.added_at(f.plan_path, "Ship it", n) for n in (0, 1)]
    assert [b[1][:10] if b else None for b in births] == ["2026-10-02", "2026-10-05"]
