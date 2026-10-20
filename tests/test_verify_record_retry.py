"""The flake rule: a failed full run whose only failures are
approved, unexpired `timing-flake` entries in local main's `tests/quarantine.toml` reruns exactly
those node ids once, serially, and is certified only through that linked retry record. An unknown
failure is never retried.

The fixture runs real pytest under the project's own `[tool.pytest.ini_options]` (`-q -n 8 --dist
loadgroup`), copied from this repo's pyproject, with a deliberate lost-update pair: two tests that
both read a shared file before either writes it. In parallel one update is lost and its test
fails; run alone it passes (the attempt count runs with the real
configuration)."""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

from agent_cli import land, verify_record, verify_retry, verify_run
from land_helpers import git as _git

REPO = Path(__file__).resolve().parents[1]
NODE = "tests/test_flaky.py::test_lost_update_a"
BROKEN = "tests/test_broken.py::test_always_fails"
PYTEST = f"{Path(sys.executable).as_posix()} -m pytest"
REGISTRY = """version: 1
repos:
  - name: Harness
    short: harness
    path: .
    remote: git@github.com:example/harness.git
    default_branch: main
    harness: "none"
    dispatch:
{env}      verify:
        - command: "echo lint ok"
          timeout_seconds: 20
        - command: "{pytest}"
          timeout_seconds: 120
      verify_timeout_seconds: 300
    notes: ""
"""
# Both tests read the book before either writes it; `b` writes last, so `a`'s entry is lost.
# Run alone (no xdist worker), nothing waits and each test passes.
FLAKY = """import json, os, time
from pathlib import Path

import pytest

SHARED = Path(os.environ.get("PYTEST_DEBUG_TEMPROOT", ".")) / "lost-update"
PARALLEL = bool(os.environ.get("PYTEST_XDIST_WORKER"))


def _wait(path, limit=30.0):
    end = time.monotonic() + limit
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.01)


def _append(me, other):
    SHARED.mkdir(exist_ok=True)
    book = SHARED / "book.json"
    seen = json.loads(book.read_text()) if book.exists() else []
    (SHARED / f"{me}.read").write_text("")
    if PARALLEL:
        _wait(SHARED / f"{other}.read")
        if me == "b":
            _wait(SHARED / "a.wrote")
    book.write_text(json.dumps(seen + [me]))
    (SHARED / f"{me}.wrote").write_text("")
    if PARALLEL and me == "a":
        _wait(SHARED / "b.wrote")
    assert me in json.loads(book.read_text()), "lost update"


@pytest.mark.xdist_group("timing-0")
def test_lost_update_a():
    _append("a", "b")


def test_lost_update_b():
    _append("b", "a")
"""


ALWAYS_FAILS = (
    "def test_lost_update_a():\n    assert False, 'lost update'\n\n\ndef test_lost_update_b():\n    pass\n"
)


def _ini() -> str:
    """This repo's own pytest configuration, verbatim."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r"^\[tool\.pytest\.ini_options\]\n.*?(?=^\[)", text, re.M | re.S)
    assert found is not None
    return found.group(0)


def _registry(root: Path, env: dict[str, str] | None = None, pytest: str = PYTEST) -> None:
    rows = "".join(f'        {k}: "{v}"\n' for k, v in (env or {}).items())
    block = f"      env:\n{rows}" if rows else ""
    (root / "agent.yaml").write_text(REGISTRY.format(env=block, pytest=pytest), encoding="utf-8")


def _entry(node: str, approval: str = "approved 2026-10-13 test", expires: str = "2099-01-01") -> str:
    return (
        f'[[quarantine]]\nnode = "{node}"\ndisposition = "timing-flake"\nowner = "maintainers"\n'
        f'reason = "lost update under xdist"\nexpires = {expires}\nevidence = "this test"\n'
        f'owner_approval = "{approval}"\n'
    )


def _quarantine(root: Path, *entries: str, branch: str | None = None) -> None:
    (root / "tests" / "quarantine.toml").write_text("version = 1\n\n" + "\n".join(entries))
    if branch:
        _git(root, "switch", "-q", "-c", branch)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "quarantine")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "harness"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_text("")
    (root / "tests").mkdir()
    (root / ".gitignore").write_text("/agent.yaml\n.agent/\n__pycache__/\n.pytest_cache/\n")
    (root / "pyproject.toml").write_text(_ini())
    (root / "tests" / "test_flaky.py").write_text(FLAKY)
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    _registry(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.setenv("AGENT_ROOT", str(root))
    return root


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The command lines of every isolated run, in order."""
    seen: list[str] = []
    real = verify_run.run

    def spy(*args, **kw):
        config = kw.get("config")
        seen.append(" | ".join(c.command for c in config.commands) if config is not None else "full")
        return real(*args, **kw)

    monkeypatch.setattr(verify_run, "run", spy)
    return seen


@pytest.fixture
def serial(harness: Path) -> Path:
    """The harness without xdist, where `NODE` always fails: for cases where only the retry
    decision after a failed run is under test, not the parallel run itself."""
    (harness / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (harness / "tests" / "test_flaky.py").write_text(ALWAYS_FAILS)
    _git(harness, "commit", "-q", "-am", "serial")
    return harness


def _store(root: Path) -> Path:
    return verify_record.store(root)


def _log(root: Path, run_id: str) -> str:
    return (_store(root) / "runs" / run_id / "output.log").read_text()


def test_an_approved_flake_is_retried_once_serially_and_linked(harness: Path, runs: list[str]):
    """The retry runs exactly the failed id whatever the configured targets (`pytest tests`), and
    without `PYTEST_ADDOPTS`, which would make it parallel again and clash with `-p no:xdist`."""
    _registry(harness, {"PYTEST_ADDOPTS": "-n 8"}, pytest=PYTEST + " tests")
    _quarantine(harness, _entry(NODE))
    record = verify_record.record(harness, "HEAD")
    retry = record["retry"]
    # one original run under the project's own -n 8, plus exactly one serial retry of one node
    assert runs[0] == "full" and len(runs) == 2
    assert runs[1] == (
        f"{PYTEST} tests --collect-only -q -o addopts= -p no:xdist"
        f" | {PYTEST} -o addopts= -p no:xdist -p no:rerunfailures {NODE}"
    )
    original = _log(harness, record["run_id"])
    assert "bringing up nodes" in original and f"FAILED {NODE}@timing-0" in original
    assert "1 failed, 2 passed" in original
    again = _log(harness, retry["run_id"])
    assert re.search(r"\b1 passed\b", again) and "bringing up nodes" not in again
    # the records are linked, and the failed run's record and evidence stay failed
    failed = json.loads((_store(harness) / "failed" / f"{record['run_id']}.json").read_text())
    assert failed["kind"] == "failed" and failed["failed_nodes"] == [NODE]
    kept = json.loads((_store(harness) / "retry" / f"{retry['run_id']}.json").read_text())
    assert kept["kind"] == "retry-passed" and kept["failed_run"] == record["run_id"]
    assert kept["failed_record"] == f"{record['run_id']}.json" and kept["nodes"] == [NODE]
    assert retry["source"] == _git(harness, "rev-parse", "main")
    found, why = verify_record.lookup(harness, "HEAD")
    assert found is not None, why
    assert land.record_kind(found) == "retry-passed"
    # a reader re-derives the retry: tampered retry evidence fails closed
    log = _store(harness) / "runs" / retry["run_id"] / "output.log"
    log.write_text(log.read_text() + "1 failed\n")
    assert "retry evidence (output.log)" in verify_record.lookup(harness, "HEAD")[1]
    assert verify_retry.launcher("uv run pytest tests -x") == "uv run pytest"


def test_a_failure_without_a_live_entry_keeps_the_gate_failing(serial: Path, runs: list[str]):
    """With no approved, unexpired entry nothing is retried and no record is written: one real run
    with a pending entry; a missing or expired entry takes the same `approved_for` path."""
    on = date(2026, 10, 13)
    for entry in ("", _entry(NODE, approval="pending"), _entry(NODE, expires="2020-01-01")):
        assert verify_retry.approved_for([NODE], verify_retry.parse("version = 1\n" + entry)[0], on) == (
            [],
            [NODE],
        )
    assert verify_retry.approved_for([NODE], verify_retry.parse("version = 1\n" + _entry(NODE))[0], on) == (
        [NODE],
        [],
    )
    _quarantine(serial, _entry(NODE, approval="pending"))
    with pytest.raises(verify_record.RecordFailed) as caught:
        verify_record.record(serial, "HEAD")
    assert "not retried: no approved, unexpired quarantine entry" in str(caught.value)
    assert runs == ["full"]
    assert verify_record.lookup(serial, "HEAD")[0] is None
    assert not (_store(serial) / "retry").exists()
    (failed,) = (_store(serial) / "failed").glob("*.json")
    assert json.loads(failed.read_text())["failed_nodes"] == [NODE]


def test_an_unknown_failure_beside_an_approved_flake_is_never_retried(serial: Path, runs: list[str]):
    (serial / "tests" / "test_broken.py").write_text("def test_always_fails():\n    assert False\n")
    _quarantine(serial, _entry(NODE))
    with pytest.raises(verify_record.RecordFailed, match=re.escape(f"quarantine entry for {BROKEN}")):
        verify_record.record(serial, "HEAD")
    assert runs == ["full"]


def test_an_approval_only_on_the_branch_being_certified_does_not_count(serial: Path, runs: list[str]):
    _quarantine(serial, _entry(NODE), branch="approves-itself")
    _git(serial, "switch", "-q", "main")
    with pytest.raises(verify_record.RecordFailed, match="not retried"):
        verify_record.record(serial, "approves-itself")
    assert runs == ["full"]


def test_a_failing_retry_is_recorded_and_the_gate_stays_failed(serial: Path, runs: list[str]):
    (serial / "tests" / "test_broken.py").write_text("def test_always_fails():\n    assert False\n")
    _quarantine(serial, _entry(NODE), _entry(BROKEN))
    with pytest.raises(verify_record.RecordFailed, match="retry-failed"):
        verify_record.record(serial, "HEAD")
    assert len(runs) == 2 and runs[1].endswith(f"{BROKEN} {NODE}")
    (kept,) = (_store(serial) / "retry").glob("*.json")
    retry = json.loads(kept.read_text())
    assert retry["kind"] == "retry-failed" and retry["nodes"] == [BROKEN, NODE]
    assert (_store(serial) / "failed" / f"{retry['failed_run']}.json").is_file()
    assert verify_record.lookup(serial, "HEAD")[0] is None


def test_failed_node_ids_are_read_only_from_a_consistent_summary():
    log = (
        "F.\n"
        "FAILED tests/test_a.py::test_x[a b - c]@timing-1 - AssertionError: no\n"
        "ERROR tests/test_b.py::test_y - fixture\n"
        "1 failed, 5 passed, 1 error in 3.21s\n"
    )
    assert verify_retry.failed_nodes(log) == (["tests/test_a.py::test_x[a b", "tests/test_b.py::test_y"], "")
    assert verify_retry.failed_nodes(log.replace("1 error", "2 errors"))[1].startswith("the summary counts 3")
    collection = "ERROR tests/test_c.py\n1 error in 0.1s\n"
    assert "names no test" in verify_retry.failed_nodes(collection)[1]
    assert verify_retry.failed_nodes("nothing")[1] == "the output has no pytest summary"


def test_quarantine_entries_are_strict_and_doctor_names_expired_and_pending(tmp_path: Path):
    good = _entry("tests/test_a.py::test_x")
    entries, problems = verify_retry.parse("version = 1\n" + good)
    assert problems == [] and entries[0].live(date(2026, 10, 13))
    _, problems = verify_retry.parse("version = 1\n" + good.replace("timing-flake", "isolation-leak"))
    assert problems == ["entry 1: the only disposition is 'timing-flake'"]
    _, problems = verify_retry.parse("version = 1\n" + good.replace("test_x", "test_x@timing-0"))
    assert "without an xdist group suffix" in problems[0]
    (tmp_path / "tests").mkdir()
    (tmp_path / verify_retry.QUARANTINE).write_text(
        "version = 1\n"
        + _entry("tests/test_a.py::test_old", expires="2026-10-01")
        + _entry("tests/test_b.py::test_new", approval="pending")
    )
    rows = verify_retry.doctor_rows(tmp_path, date(2026, 10, 13))
    assert rows == [
        ("warn", "quarantine expired, these gate normally again: tests/test_a.py::test_old"),
        ("warn", "quarantine awaiting owner approval, not retried: tests/test_b.py::test_new"),
    ]


def test_the_shipped_quarantine_parses_and_is_approved():
    entries, problems = verify_retry.parse((REPO / verify_retry.QUARANTINE).read_text(encoding="utf-8"))
    assert problems == []
    assert all(e.approved() for e in entries)  # the owner approved the seeded entries (D4b, 068)
    for e in entries:
        path, name = e.node.split("::")
        assert f"def {name}(" in (REPO / path).read_text(encoding="utf-8")


# ----------------------------------------------------------------
CRASH = "import os\n\ndef test_lost_update_a():\n    if os.environ.get('PYTEST_XDIST_WORKER'):\n        os._exit(23)\n"


def test_a_worker_crash_is_not_a_timing_flake_and_cannot_hide_an_unrun_defect(harness: Path, runs: list[str]):
    """Item 1: an approved node that kills its xdist worker is a crash, not a flake. The probe: one
    worker, no restarts; the crash comes before an unknown failing test runs. Nothing is retried or
    certified, so the push gate refuses."""
    from agent_cli import push_records
    from agent_cli.push_input import Update

    (harness / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-n 1 --max-worker-restart=0"\n'
    )
    (harness / "tests" / "test_flaky.py").write_text(
        CRASH + "\ndef test_unknown_defect():\n    assert False\n"
    )
    _quarantine(harness, _entry(NODE))
    with pytest.raises(verify_record.RecordFailed, match="did not execute every test"):
        verify_record.record(harness, "HEAD")
    assert runs == ["full"]
    sha = _git(harness, "rev-parse", "HEAD")
    assert push_records.problems(harness, [Update(sha, "0" * 40, "refs/heads/main", "refs/heads/main")])


def test_an_original_run_that_executed_fewer_tests_than_collected_fails_the_retry():
    collect = "tests/test_a.py::test_x\ntests/test_a.py::test_y\n3 tests collected in 0.01s\n"
    assert verify_retry.incomplete("1 failed, 2 passed in 1.0s\n", collect) == ""
    assert "executed 2 of 3" in verify_retry.incomplete("1 failed, 1 passed in 1.0s\n", collect)
    assert "cannot be read" in verify_retry.incomplete("no summary", collect)
    for marker in ("[gw0] node down: Not properly terminated", "worker 'gw1' crashed while running x"):
        assert "did not execute every test" in verify_retry.failed_nodes(marker + "\n1 failed in 1s\n")[1]
