"""The suite cost plugin (suite_cost.py), run over a probe suite in a child pytest."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import suite_cost
from suite_cost_site import sitecustomize as site

TESTS = Path(__file__).resolve().parent

PROBE = """
import os, subprocess, sys

def python(*args, **kw):
    subprocess.run([sys.executable, *args], check=True, **kw)

def test_two():
    python("-c", "pass")
    python("-c", "pass")

def test_a_child_that_starts_three():
    python("-c", "import subprocess, sys\\nfor _ in range(3): subprocess.run([sys.executable, '-c', 'pass'])")

def test_an_isolated_child():
    python("-I", "-c", "import subprocess, sys; subprocess.run([sys.executable, '-c', 'pass'])")

def test_a_child_without_the_pythonpath():
    python("-c", "pass", env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})

def test_a_child_whose_own_sitecustomize_still_runs():
    code = "import subprocess, sys\\nsubprocess.run([sys.executable, '-c', 'pass'])\\nprint(sys.own_hook_ran)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True", out.stderr
"""

# Three starts in every pytest process, before any test: the exact outside-test count.
PROBE_CONFTEST = """
import subprocess, sys

def pytest_sessionstart(session):
    for _ in range(3):
        subprocess.run([sys.executable, "-c", "pass"], check=True)
"""

# The child's own `sitecustomize`, which the counter's directory shadows on PYTHONPATH.
OWN_HOOK = "import sys\nsys.own_hook_ran = True\n"


def _pytest(tmp_path: Path, *args: str, stats: Path | None, code: int = 0) -> str:
    """A child pytest in `tmp_path` with only this plugin, outside any measurement of this suite."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    hooks = tmp_path.parent / f"{tmp_path.name}-hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "sitecustomize.py").write_text(OWN_HOOK, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in (suite_cost.STATS_ENV, site.SPAWNS_ENV)}
    path = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != site.SITE_DIR]
    env["PYTHONPATH"] = os.pathsep.join([str(TESTS), str(hooks), *path])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if stats is not None:
        env[suite_cost.STATS_ENV] = str(stats)
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "suite_cost", "-p", "no:cacheprovider", *args]
    out = subprocess.run(
        argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert out.returncode == code, out.stdout + out.stderr
    return out.stdout


def _probe(tmp_path: Path, *args: str) -> dict[str, Any]:
    (tmp_path / "test_probe.py").write_text(PROBE, encoding="utf-8")
    (tmp_path / "conftest.py").write_text(PROBE_CONFTEST, encoding="utf-8")
    stats = tmp_path / "out" / "stats.json"
    _pytest(tmp_path, *args, stats=stats)
    return json.loads(stats.read_text(encoding="utf-8"))


def _platform_starts() -> int:
    """The processes `platform.platform()` starts in a fresh interpreter, counted without the plugin."""
    code = (
        "import platform, subprocess\n"
        "n, real = [0], subprocess.Popen.__init__\n"
        "def counting(self, *a, **kw):\n"
        "    n[0] += 1\n"
        "    real(self, *a, **kw)\n"
        "subprocess.Popen.__init__ = counting\n"
        "platform.platform()\n"
        "print(n[0])\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    return int(out.stdout)


def _rows(stats: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["test"].split("::")[-1]: row for row in stats["tests"]}


@pytest.fixture(scope="module")
def serial(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _probe(tmp_path_factory.mktemp("serial"), "-p", "no:xdist")


def test_each_test_counts_its_own_starts_and_those_of_the_python_children_it_can_follow(
    serial: dict[str, Any],
):
    stats = serial
    rows = _rows(stats)
    assert rows["test_two"]["call"]["spawns"] == 2 and not rows["test_two"]["opaque"]
    assert rows["test_a_child_that_starts_three"]["call"]["spawns"] == 4
    assert not rows["test_a_child_that_starts_three"]["opaque"]
    assert rows["test_two"]["setup"]["spawns"] == rows["test_two"]["teardown"]["spawns"] == 0
    totals = stats["totals"]
    assert totals["tests"] == 5
    assert totals["spawns_outside_tests"] == 3
    assert totals["spawns"] == sum(r["spawns"] for r in rows.values()) + 3 == 13
    assert totals["call_spawns_p99"] == 4
    assert totals["test_seconds"] > 0


def test_a_child_s_own_sitecustomize_runs_while_the_counter_follows_it(serial: dict[str, Any]):
    # The probe asserts the hook ran; two starts here means the child counted its own as well.
    row = _rows(serial)["test_a_child_whose_own_sitecustomize_still_runs"]
    assert row["call"]["spawns"] == 2 and not row["opaque"]


def test_a_python_child_it_cannot_follow_marks_the_test_opaque(serial: dict[str, Any]):
    stats = serial
    rows = _rows(stats)
    assert rows["test_an_isolated_child"]["opaque"] and rows["test_an_isolated_child"]["call"]["spawns"] == 1
    assert rows["test_a_child_without_the_pythonpath"]["opaque"]
    if suite_cost.resource is not None:
        totals = stats["totals"]
        opaque_cpu = sum(r["cpu_s"] for r in rows.values() if r["opaque"])
        assert totals["opaque_cpu_s"] == round(opaque_cpu, 3)
        assert 0 < totals["opaque_share"] < 1


def test_two_xdist_workers_sum_in_the_written_file(tmp_path: Path):
    stats = _probe(tmp_path, "-n", "2", "--dist", "each")
    rows, workers = _rows(stats), stats["workers"]
    assert set(workers) == {"main", "gw0", "gw1"}
    # Each worker: 10 in its tests, 3 at session start, and what xdist's `platform.platform()`
    # starts (`uname -p` and `file -b` on macOS). The controller: 3, and the two workers.
    worker = 13 + _platform_starts()
    assert workers["gw0"]["spawns"] == workers["gw1"]["spawns"] == worker
    assert workers["main"]["spawns"] == 5
    assert rows["test_two"]["call"]["spawns"] == 4
    assert rows["test_a_child_that_starts_three"]["call"]["spawns"] == 8
    totals = stats["totals"]
    assert sum(r["spawns"] for r in rows.values()) == 20
    assert totals["spawns"] == 2 * worker + 5 and totals["spawns_outside_tests"] == 2 * worker + 5 - 20


# A real verification: a `-I` guardian whose command, watchdog and Git shims start their own.
VERIFIER_PROBE = """
import subprocess
import pytest
import suite_cost
from verification_fixtures import config, git, start
from agent_cli.dispatch import verification as runner

@pytest.fixture(autouse=True)
def guarded_popen(monkeypatch):
    # What the repository's Obsidian guard does to every test: a subclass in Popen's place.
    class GuardedPopen(subprocess.Popen):
        def __init__(self, args, *rest, **kw):
            super().__init__(args, *rest, **kw)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

@pytest.fixture
def layout(tmp_path):
    root, worktree = tmp_path / "harness", tmp_path / "worktree"
    root.mkdir()
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    (worktree / "tracked").write_text("original\\n")
    git(worktree, "add", "tracked")
    git(worktree, "-c", "user.name=F", "-c", "user.email=f@example.invalid", "commit", "-m", "f")
    return root, worktree, git(worktree, "rev-parse", "HEAD")

def test_a_verifier_run(layout):
    counter = suite_cost.counter
    before = counter.count
    result = runner.wait(start(layout, config("printf ok")))
    assert result.cleanup_confirmed and result.evidence.commands[0].exit_code == 0, result
    with open("in_process", "w") as fh:  # the child pytest's cwd
        fh.write(str(counter.count - before))
"""


@pytest.mark.skipif(os.name == "nt", reason="Windows starts the roles through launch_python's job path")
def test_the_harness_s_isolated_verifier_children_count_their_own_starts(tmp_path: Path):
    (tmp_path / "test_verifier.py").write_text(VERIFIER_PROBE, encoding="utf-8")
    stats_file = tmp_path / "out" / "stats.json"
    _pytest(tmp_path, "-p", "no:xdist", stats=stats_file)
    stats = json.loads(stats_file.read_text(encoding="utf-8"))
    row = _rows(stats)["test_a_verifier_run"]
    # The test process starts the guardian and some `ps` shims itself; the guardian's command,
    # watchdog and Git shims, and the watchdog's own, reach the row through the spawn file.
    in_process = int((tmp_path / "in_process").read_text(encoding="utf-8"))
    assert not row["opaque"] and stats["totals"]["opaque_tests"] == 0
    assert row["call"]["spawns"] >= in_process + 3, (row, in_process)


def test_the_harness_s_isolated_children_start_unchanged_when_not_measuring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli.dispatch import verification_process as procs

    counter = site.active()
    monkeypatch.setattr(counter, "watch", False)
    started: list[list[str]] = []

    def record(args: Any, kw: dict[str, Any]) -> None:
        started.append(list(args))
        raise _Recorded

    monkeypatch.setattr(counter, "listeners", [*counter.listeners, record])
    with pytest.raises(_Recorded):
        procs.launch("guardian", env=procs.BASE_ENV, cwd=tmp_path)
    if os.name != "nt":
        with pytest.raises(_Recorded):
            procs.capture(["/bin/echo"], tmp_path, procs.BASE_ENV, 5)
    for argv in started:
        assert argv[1:3] == ["-I", "-c"]
        assert site.CHILD_MARK not in argv[3] and "suite_cost" not in argv[3]
    assert started[0][3].startswith("import site;" if os.name == "nt" else "import sys, importlib.util as u;")
    assert len(started) == (1 if os.name == "nt" else 2)


class _Recorded(Exception):
    """Raised by a listener in place of the start it was shown."""


def test_without_the_variable_nothing_is_written_but_children_are_still_counted(tmp_path: Path):
    tmp_path = tmp_path / "quiet"  # the autouse fixtures write next to it
    tmp_path.mkdir()
    (tmp_path / "test_quiet.py").write_text(
        "import os\n"
        "def test_the_child_counter_is_on():\n"
        f"    assert os.environ[{site.SPAWNS_ENV!r}]\n"
        f"    assert {site.SITE_DIR!r} in os.environ.get('PYTHONPATH', '').split(os.pathsep)\n",
        encoding="utf-8",
    )
    _pytest(tmp_path, "-p", "no:xdist", stats=None)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pytest.ini", "test_quiet.py"]


CEILING_PROBE = """
import subprocess, sys
import suite_cost

def python(n):
    for _ in range(n):
        subprocess.run([sys.executable, "-c", "pass"], check=True)

def test_at_the_ceiling():
    python(2)

def test_over_the_ceiling():
    python(3)

def test_over_the_ceiling_in_a_child():
    python(1)
    subprocess.run([sys.executable, "-c", "import subprocess, sys; subprocess.run([sys.executable, '-c', 'pass'])"])
    python(1)

def test_over_its_own_slow_limit():
    python(2)

def test_under_its_raised_slow_limit():
    python(4)

def test_set_aside_starts_are_not_its_own():
    with suite_cost.uncounted():
        python(4)
        subprocess.run([sys.executable, "-c", "import subprocess, sys; subprocess.run([sys.executable, '-c', 'pass'])"])
    python(2)

def test_one_extra_start_beside_set_aside_work():
    with suite_cost.uncounted():
        python(4)
    python(3)
"""

# The ceiling lowered so the probe stays small; `[slow]` keys carry the group suffix pass records show.
CEILING_CONFTEST = "import suite_cost\nsuite_cost.CEILING = 2\n"
CEILING_BUDGET = """[suite]
tests_max = 10
spawns_max = 100
headroom_pct = 3
[slow]
"test_probe.py::test_over_its_own_slow_limit@timing-0" = 1
"test_probe.py::test_under_its_raised_slow_limit" = 4
"""


@pytest.mark.parametrize("measured", [False, True])
def test_the_ceiling_fails_a_test_over_its_limit_and_names_it(tmp_path: Path, measured: bool):
    (tmp_path / "test_probe.py").write_text(CEILING_PROBE, encoding="utf-8")
    (tmp_path / "conftest.py").write_text(CEILING_CONFTEST, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "budget.toml").write_text(CEILING_BUDGET, encoding="utf-8")
    stats = tmp_path / "out" / "stats.json" if measured else None
    out = _pytest(tmp_path, "-p", "no:xdist", "-rf", stats=stats, code=1)
    failed = sorted(line.split(" (")[0] for line in out.splitlines() if line.startswith("spawn-ceiling: "))
    assert failed == [
        "spawn-ceiling: test_probe.py::test_one_extra_start_beside_set_aside_work started 3 processes in its call "
        "phase, over its limit of 2",
        "spawn-ceiling: test_probe.py::test_over_its_own_slow_limit started 2 processes in its call phase, "
        "over its limit of 1",
        "spawn-ceiling: test_probe.py::test_over_the_ceiling started 3 processes in its call phase, over its limit of 2",
        "spawn-ceiling: test_probe.py::test_over_the_ceiling_in_a_child started 4 processes in its call phase, "
        "over its limit of 2",
    ], out
    assert "limit of 1 (its [slow] entry in tests/budget.toml)" in out
    assert "4 failed, 3 passed" in out
    if stats is not None:
        rows = _rows(json.loads(stats.read_text(encoding="utf-8")))
        set_aside = rows["test_set_aside_starts_are_not_its_own"]["call"]
        assert (set_aside["spawns"], set_aside["uncounted"]) == (2, 6)
