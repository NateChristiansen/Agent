"""Benchmark the full test suite: N runs, each with comparable metadata.

Usage: uv run python scripts/suite_bench.py [--runs N] [--label TEXT] [--out DIR] [-- pytest args]

Each run gets a fresh temp root (PYTEST_DEBUG_TEMPROOT) under the suite's own retention policy.
`files_created` counts the paths the test processes created while they ran, deleted or not
(scripts/bench_count_plugin.py; files git creates are not seen, so it compares runs);
`files_retained` counts what the run left behind, whose deletion is timed as `cleanup_s`. Per run
it records the head, the exact pytest command, worker count, temp backend, load average, wall
time, summed setup/call/teardown time, and the collected/passed/failed/skipped counts, as one JSON
line in `<out>/bench.jsonl`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_DURATION = re.compile(r"^\s*([\d.]+)s (setup|call|teardown)\s")
_SUMMARY = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout.strip()


def _workers(pytest_args: list[str]) -> str:
    for i, arg in enumerate(pytest_args):
        if arg == "-n" and i + 1 < len(pytest_args):
            return pytest_args[i + 1]
        if arg.startswith("-n") and len(arg) > 2:
            return arg[2:]
        if arg.startswith("--numprocesses="):
            return arg.split("=", 1)[1]
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'addopts\s*=\s*"[^"]*?-n\s+([^\s"]+)', text)
    return found.group(1) if found else "?"


def _count_files(root: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(root))


def run_once(pytest_args: list[str], label: str, base: Path) -> dict[str, object]:
    temp = Path(tempfile.mkdtemp(prefix="suite-bench-", dir=base))
    junit = temp / "junit.xml"
    counts_dir = temp / "counts"
    counts_dir.mkdir()
    scripts = str(Path(__file__).resolve().parent)
    env = {
        **os.environ,
        "PYTEST_DEBUG_TEMPROOT": str(temp / "pytest"),
        "BENCH_COUNT_DIR": str(counts_dir),
        "PYTHONPATH": os.pathsep.join(p for p in (scripts, os.environ.get("PYTHONPATH", "")) if p),
    }
    (temp / "pytest").mkdir()
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "bench_count_plugin",
        "--durations=0",
        f"--junitxml={junit}",
        *pytest_args,
    ]
    load_before = os.getloadavg()
    started = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    wall = time.monotonic() - started
    phases = {"setup": 0.0, "call": 0.0, "teardown": 0.0}
    for line in proc.stdout.splitlines():
        found = _DURATION.match(line)
        if found:
            phases[found.group(2)] += float(found.group(1))
    counts: dict[str, int] = {}
    tail = proc.stdout.strip().splitlines()[-1:] or [""]
    for number, kind in _SUMMARY.findall(tail[0]):
        counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(number)
    collected = 0
    if junit.exists():
        collected = sum(1 for _ in ET.parse(junit).iter("testcase"))  # noqa: S314 -- our own junit file
    created = sum(int(f.read_text() or 0) for f in counts_dir.iterdir())
    retained = _count_files(temp / "pytest")
    clean_started = time.monotonic()
    shutil.rmtree(temp, ignore_errors=True)
    cleanup = time.monotonic() - clean_started
    return {
        "label": label,
        "head": _git("rev-parse", "--short", "HEAD"),
        "command": " ".join(argv[2:]).replace(str(junit), "<junit>"),
        "workers": _workers(pytest_args),
        "temp_backend": str(base),
        "load_before": [round(x, 2) for x in load_before],
        "load_after": [round(x, 2) for x in os.getloadavg()],
        "exit": proc.returncode,
        "wall_s": round(wall, 1),
        "setup_s": round(phases["setup"], 1),
        "call_s": round(phases["call"], 1),
        "teardown_s": round(phases["teardown"], 1),
        "collected": collected,
        "counts": counts,
        "files_created": created,
        "files_retained": retained,
        "cleanup_s": round(cleanup, 1),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--label", default="")
    parser.add_argument("--out", default=".")
    parser.add_argument("--temp-base", default=tempfile.gettempdir(), help="where run temp roots go")
    args, rest = parser.parse_known_args()
    pytest_args = [a for a in rest if a != "--"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for i in range(args.runs):
        row = run_once(pytest_args, args.label, Path(args.temp_base))
        with (out / "bench.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        print(f"run {i + 1}/{args.runs}: {json.dumps(row)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
