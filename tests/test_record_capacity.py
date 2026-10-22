"""Pass-record runs share the verification lanes and run single-flight per key (C9).

A repo-wide lock refused every second record request, so two lanes could never run two records,
and record runs held no lane at all, so a landing's full run
overlapped dispatch verification."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_cli import verify_record, verify_run
from harness_root import _no_auto_maintenance  # pyright: ignore[reportPrivateUsage]
from land_helpers import git as _git

PASS = "echo 3 passed in 0.01s"
REGISTRY = """version: 1
repos:
  - name: Harness
    short: harness
    path: .
    remote: git@github.com:example/harness.git
    default_branch: main
    harness: "none"
    dispatch:
      max_concurrent_verify: {lanes}
      verify:
        - command: "{command}"
          timeout_seconds: 20
      verify_timeout_seconds: 30
    notes: ""
"""


@pytest.fixture(scope="session")
def _record_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One committed file; built once per worker and copied per test (verify speed, phase 2)."""
    root = tmp_path_factory.mktemp("record-template") / "harness"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_text("")
    (root / ".gitignore").write_text("/agent.yaml\n.agent/\n")
    (root / "tracked.txt").write_text("v1\n")
    _git(root, "init", "-q", "-b", "main", "--template=")
    # the commit would start a detached `git maintenance run --auto` that can still hold
    # objects/maintenance.lock while `harness` copies the template
    _no_auto_maintenance(root / ".git" / "config")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _record_template: Path) -> Path:
    root = tmp_path / "harness"
    shutil.copytree(_record_template, root, symlinks=True)
    monkeypatch.setenv("AGENT_ROOT", str(root))
    return root


def _lanes(root: Path, lanes: int) -> None:
    (root / "agent.yaml").write_text(REGISTRY.format(lanes=lanes, command=PASS), encoding="utf-8")


@pytest.fixture
def overlap(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Counts record runs and the most that were inside `verify_run.run` at once."""
    seen = {"runs": 0, "now": 0, "most": 0}
    real = verify_run.run
    lock = threading.Lock()

    def run(*args: Any, **kwargs: Any) -> Any:
        with lock:
            seen["runs"] += 1
            seen["now"] += 1
            seen["most"] = max(seen["most"], seen["now"])
        try:
            time.sleep(0.6)
            return real(*args, **kwargs)
        finally:
            with lock:
                seen["now"] -= 1

    monkeypatch.setattr(verify_run, "run", run)
    return seen


def _together(*calls: tuple[Path, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{} for _ in calls]

    def go(i: int, root: Path, rev: str) -> None:
        out[i] = verify_record.record(root, rev)

    threads = [threading.Thread(target=go, args=(i, root, rev)) for i, (root, rev) in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return out


def test_identical_requests_share_one_run(harness: Path, overlap: dict[str, int]):
    _lanes(harness, 2)
    first, second = _together((harness, "HEAD"), (harness, "HEAD"))
    assert overlap["runs"] == 1
    assert first["run_id"] == second["run_id"] and {first["reused"], second["reused"]} == {True, False}


def _second_tree(root: Path) -> str:
    _git(root, "switch", "-q", "-c", "other")
    (root / "tracked.txt").write_text("v2\n")
    _git(root, "commit", "-q", "-am", "another tree")
    sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-q", "main")
    return sha


def test_two_lanes_run_two_different_trees_at_once(harness: Path, overlap: dict[str, int]):
    _lanes(harness, 2)
    other = _second_tree(harness)
    _together((harness, "main"), (harness, other))
    assert overlap["runs"] == 2 and overlap["most"] == 2


def test_one_lane_runs_them_one_after_the_other(harness: Path, overlap: dict[str, int]):
    _lanes(harness, 1)
    other = _second_tree(harness)
    _together((harness, "main"), (harness, other))
    assert overlap["runs"] == 2 and overlap["most"] == 1
