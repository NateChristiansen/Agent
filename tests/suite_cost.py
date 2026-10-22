"""Processes each test starts, so the suite can be budgeted by spawns instead of wall time.

The counter is the one wrapper on `subprocess.Popen.__init__` (`suite_cost_site/sitecustomize.py`);
the `spawns` fixture in `test_spawn_cost.py` listens on it rather than patching `Popen` a second
time. The per-test ceiling fails a passing test whose call phase started more than `CEILING`
processes, or more than its own `[slow]` value in `tests/budget.toml` (`agent_cli.suite_budget`).
It runs in every pytest session, measured or not; the suite's own totals are budgeted at landing.

With `AGENT_SUITE_STATS=<file>` the controller writes one JSON file at the end of the session:

- `totals`: `spawns` (every process, in tests or not), `spawns_outside_tests`, `test_seconds` (the
  phase durations summed), `cpu_children_s` (RUSAGE_CHILDREN of every pytest process that ran
  tests; absent on Windows), `opaque_share` and `call_spawns_p99`;
- `workers`: the same totals for each pytest process;
- `tests`: one row per node id, with `setup`, `call` and `teardown` each holding `spawns`, `s` and
  `cpu_s`.

Python children that inherit the environment count their own starts too: the controller puts
`suite_cost_site` first on `PYTHONPATH`, and each pytest process names its spawn file in
`AGENT_SUITE_STATS_SPAWNS`. A test that starts a Python child this cannot follow (`-I`, `-E`, `-S`,
or an `env=` without the two) is `opaque`; `opaque_share` is those tests' child CPU over
`cpu_children_s`, the part of the suite's process cost the spawn count may miss.

Without the variable the children are still followed, for the ceiling, and nothing is written. A
pytest started by a measured test inherits the spawn file and leaves its starts to the parent's.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli import suite_budget
from suite_cost_site import sitecustomize as site

try:
    import resource
except ImportError:  # Windows
    resource = None

STATS_ENV = "AGENT_SUITE_STATS"
KEY = "suite_cost"
PHASES = ("setup", "call", "teardown")
#: Call-phase starts a test may make unless `[slow]` names it; the owner set it from the 2026-10-22 runs.
CEILING = 100

counter: Any = site.active()


def _cpu() -> float | None:
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


class _Process:
    """One pytest process's starts: its own, and those its Python children appended to its file."""

    def __init__(self, spawn_file: Path) -> None:
        self.spawn_file = spawn_file
        self.read = 0
        self.child_spawns = self.child_opaque = 0
        self.spawns = self.opaque = 0
        self.uncounted = 0
        self.cpu = 0.0

    def total(self) -> int:
        self._children()
        return counter.count + self.child_spawns

    def _children(self) -> None:
        try:
            size = self.spawn_file.stat().st_size
        except FileNotFoundError:
            return
        if size <= self.read:
            return
        with open(self.spawn_file, "rb") as fh:
            fh.seek(self.read)
            data = fh.read(size - self.read)
        self.read += len(data)
        self.child_spawns += len(data)
        self.child_opaque += data.count(b"o")

    def take(self) -> dict[str, Any]:
        """What was started since the last call, less what `uncounted` set aside."""
        spawns = self.total()
        opaque = counter.opaque + self.child_opaque
        delta: dict[str, Any] = {
            "spawns": spawns - self.spawns - self.uncounted,
            "uncounted": self.uncounted,
            "opaque": opaque - self.opaque,
        }
        self.spawns, self.opaque, self.uncounted = spawns, opaque, 0
        if (cpu := _cpu()) is not None:
            delta["cpu_s"] = cpu - self.cpu
            self.cpu = cpu
        return delta

    def totals(self) -> dict[str, Any]:
        self.take()
        out: dict[str, Any] = {"spawns": self.spawns, "opaque_spawns": self.opaque}
        if resource is not None:
            out["cpu_children_s"] = round(self.cpu, 3)
        return out


class _Controller:
    def __init__(self, output: str | None, spawn_dir: Path, pythonpath: str | None) -> None:
        self.output = output
        self.spawn_dir = spawn_dir
        self.pythonpath = pythonpath
        self.rows: dict[str, dict[str, Any]] = {}
        self.workers: dict[str, dict[str, Any]] = {}


_process: _Process | None = None
_controller: _Controller | None = None
_limits: dict[str, float] = {}


@contextmanager
def uncounted() -> Iterator[None]:
    """Leave the starts inside out of the running test's figure, so its ceiling sees only its own work.

    For a helper whose cost depends on what ran before it in the session, like a record cache
    shared across workers. The starts still count in the suite's totals, which the budget limits."""
    if _process is None:
        yield
        return
    before = _process.total()
    try:
        yield
    finally:
        _process.uncounted += _process.total() - before


def _base(nodeid: str) -> str:
    """The node id without the `@<group>` suffix xdist's loadgroup adds."""
    return re.sub(r"@[^\[\]/:]+$", "", nodeid)


def _slow(root: Path) -> dict[str, float]:
    path = root / suite_budget.PATH
    if not path.is_file():
        return {}
    budget = suite_budget.parse(path.read_text(encoding="utf-8"))
    return {_base(node): limit for node, limit in budget.slow.items()}


def configure(config: pytest.Config) -> None:
    """Called first from `conftest.pytest_configure`, so its probe child counts outside tests."""
    global _process, _controller, _limits
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        if not (spawn_dir := workerinput.get(KEY)):
            return
        spawn_file = Path(spawn_dir) / f"{workerinput['workerid']}.spawns"
    else:
        output = os.environ.pop(STATS_ENV, None)
        if not output and os.environ.get(site.SPAWNS_ENV):
            return
        spawn_dir = Path(tempfile.mkdtemp(prefix="suite-cost-"))
        pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (site.SITE_DIR, pythonpath) if p)
        _controller = _Controller(str(Path(output).resolve()) if output else None, spawn_dir, pythonpath)
        spawn_file = spawn_dir / "main.spawns"
    os.environ[site.SPAWNS_ENV] = str(spawn_file)
    counter.sink = None
    counter.watch = True
    _process = _Process(spawn_file)
    _limits = _slow(config.rootpath)


def pytest_configure(config: pytest.Config) -> None:
    configure(config)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    if _controller is not None:
        node.workerinput[KEY] = str(_controller.spawn_dir)


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    if _process is not None:
        _process.take()  # what ran between tests is not this test's


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    report = yield
    if _process is not None:
        delta = _process.take()
        report.user_properties.append((KEY, delta))
        slow = _limits.get(_base(item.nodeid))
        limit = CEILING if slow is None else slow
        if report.when == "call" and report.passed and delta["spawns"] > limit:
            report.outcome = "failed"
            report.longrepr = (
                f"spawn-ceiling: {item.nodeid} started {delta['spawns']} processes in its call phase, "
                f"over its limit of {limit:g} ({'the per-test ceiling' if slow is None else 'its [slow] entry'}"
                f" in {suite_budget.PATH}). Start fewer processes; a new or raised [slow] entry lands only "
                "with the owner's budget_raise."
            )
    return report


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _controller is None or _controller.output is None:
        return
    for name, value in report.user_properties:
        if name != KEY or not isinstance(value, dict):
            continue
        row = _controller.rows.setdefault(report.nodeid, {"test": report.nodeid, "opaque": False})
        phase = row.setdefault(report.when, {"spawns": 0, "s": 0.0})
        phase["spawns"] += value["spawns"]
        if value.get("uncounted"):
            phase["uncounted"] = phase.get("uncounted", 0) + value["uncounted"]
        phase["s"] += report.duration
        if "cpu_s" in value:
            phase["cpu_s"] = phase.get("cpu_s", 0.0) + value["cpu_s"]
        row["opaque"] = row["opaque"] or value["opaque"] > 0


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    out = getattr(node, "workeroutput", {}).get(KEY)
    if _controller is not None and out is not None:
        _controller.workers[str(node.gateway.id)] = out


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    if _process is None:
        return
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[KEY] = _process.totals()
    elif _controller is not None and _controller.output is not None:
        _write(_controller, _controller.output, _process.totals())


def pytest_unconfigure(config: pytest.Config) -> None:
    global _process, _controller
    if _controller is not None:
        if _controller.pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = _controller.pythonpath
        if _controller.output is not None:
            os.environ[STATS_ENV] = _controller.output
        shutil.rmtree(_controller.spawn_dir, ignore_errors=True)
    if _process is not None:
        os.environ.pop(site.SPAWNS_ENV, None)
        counter.watch = False
    _process = _controller = None


def _p99(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(math.ceil(0.99 * len(ordered)) - 1, 0)] if ordered else 0


def _write(controller: _Controller, output: str, own: dict[str, Any]) -> None:
    rows = []
    for nodeid in sorted(controller.rows):
        row = controller.rows[nodeid]
        phases = [row[p] for p in PHASES if p in row]
        row["spawns"] = sum(p["spawns"] for p in phases)
        row["s"] = round(sum(p["s"] for p in phases), 6)
        if any("cpu_s" in p for p in phases):
            row["cpu_s"] = round(sum(p.get("cpu_s", 0.0) for p in phases), 6)
        for p in phases:
            p["s"] = round(p["s"], 6)
            if "cpu_s" in p:
                p["cpu_s"] = round(p["cpu_s"], 6)
        rows.append(row)
    # Each worker is the controller's child, so the controller's own child CPU repeats theirs.
    workers = {"main": own, **controller.workers}
    runners = controller.workers or {"main": own}
    spawns = sum(w["spawns"] for w in workers.values())
    in_tests = sum(r["spawns"] for r in rows)
    totals: dict[str, Any] = {
        "tests": len(rows),
        "spawns": spawns,
        "spawns_outside_tests": spawns - in_tests,
        "opaque_tests": sum(r["opaque"] for r in rows),
        "test_seconds": round(sum(r["s"] for r in rows), 3),
        "call_spawns_p99": _p99([r["call"]["spawns"] for r in rows if "call" in r]),
    }
    if resource is not None:
        cpu = sum(w.get("cpu_children_s", 0.0) for w in runners.values())
        opaque_cpu = sum(r.get("cpu_s", 0.0) for r in rows if r["opaque"])
        totals["cpu_children_s"] = round(cpu, 3)
        totals["opaque_cpu_s"] = round(opaque_cpu, 3)
        totals["opaque_share"] = round(opaque_cpu / cpu, 4) if cpu > 0 else None
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.write_text(json.dumps({"totals": totals, "workers": workers, "tests": rows}, indent=1) + "\n")
    os.replace(partial, out)
