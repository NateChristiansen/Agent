"""The timing runs that decide xdist settings and `dispatch.max_concurrent_verify`.

Usage:
  uv run python scripts/lane_bench.py dist  [--runs 3] [--out DIR]
  uv run python scripts/lane_bench.py lanes [--runs 3] [--out DIR] [--scoped FILE ...]

`dist` runs the full suite under each candidate worker setting, interleaved so drifting load
hits every candidate alike: the project default (`-n 8 --dist loadgroup`), `-n auto`, and
`-n 8 --dist worksteal`. Run it on an idle machine. The faster candidate is adopted only if
every one of its runs is green; one green pass of an intermittently failing suite proves
nothing (pyproject.toml).

`lanes` measures what a second verification lane would cost a landing: the full suite alone,
then the full suite while two scoped checks run back to back beside it (each the configured
`pytest <files>`, as a dispatch's scoped selection runs). `max_concurrent_verify: 2` is
supported only if the land run's median wall time and its failure rate both rise by at most
20%. The default stays 1 until
then. Nothing here holds worker launches; the plan dropped that (rev 4, A9).

Each run is one JSON line in `<out>/lane-bench.jsonl` (suite_bench's fields plus `mode`,
`candidate`, `beside`), and the summary is printed at the end. Numbers are measurements of the
machine at the time; the load average before and after each run is recorded with it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from suite_bench import run_once  # noqa: E402 -- the sibling script, imported by path

CANDIDATES = {
    "n8-loadgroup": [],  # the project's own addopts
    "auto-loadgroup": ["-n", "auto"],
    "n8-worksteal": ["-n", "8", "--dist", "worksteal"],
}
#: A typical scoped selection: a handful of files, as `dispatch/test_select.py` picks them.
SCOPED = ["tests/test_features.py", "tests/test_tasks.py", "tests/test_status.py", "tests/test_select.py"]
LIMIT = 1.20


def _green(row: dict[str, object]) -> bool:
    counts = row.get("counts") or {}
    assert isinstance(counts, dict)
    return row.get("exit") == 0 and not counts.get("failed") and not counts.get("error")


def _write(out: Path, row: dict[str, object]) -> None:
    with (out / "lane-bench.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def dist(runs: int, out: Path, base: Path) -> str:
    rows: dict[str, list[dict[str, object]]] = {name: [] for name in CANDIDATES}
    for i in range(runs):
        for name, args in CANDIDATES.items():
            row = {**run_once(args, name, base), "mode": "dist", "candidate": name, "round": i + 1}
            rows[name].append(row)
            _write(out, row)
    lines, best = [], None
    for name, got in rows.items():
        median = statistics.median(float(str(r["wall_s"])) for r in got)
        green = all(_green(r) for r in got)
        lines.append(
            f"{name}: median {median:.0f} s over {len(got)} runs, {'all green' if green else 'NOT green'}"
        )
        if green and (best is None or median < best[1]):
            best = (name, median)
    lines.append(f"adopt: {best[0]}" if best else "adopt: none; no candidate was green in every run")
    return "\n".join(lines)


def _scoped_loop(stop: threading.Event, files: list[str], done: list[int]) -> None:
    while not stop.is_set():
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *files],
            capture_output=True,
            text=True,
            check=False,
        )
        done.append(proc.returncode)


def lanes(runs: int, out: Path, base: Path, files: list[str]) -> str:
    alone: list[dict[str, object]] = []
    beside: list[dict[str, object]] = []
    for i in range(runs):
        row = {**run_once([], "alone", base), "mode": "lanes", "beside": 0, "round": i + 1}
        alone.append(row)
        _write(out, row)
        stop, done = threading.Event(), list[int]()
        loops = [threading.Thread(target=_scoped_loop, args=(stop, files, done)) for _ in range(2)]
        for t in loops:
            t.start()
        time.sleep(5)  # the scoped checks are up before the land run starts
        row = {**run_once([], "beside-2-scoped", base), "mode": "lanes", "beside": 2, "round": i + 1}
        stop.set()
        for t in loops:
            t.join()
        row["scoped_runs"], row["scoped_failed"] = len(done), sum(1 for c in done if c)
        beside.append(row)
        _write(out, row)

    def median(rows: list[dict[str, object]]) -> float:
        return statistics.median(float(str(r["wall_s"])) for r in rows)

    def failing(rows: list[dict[str, object]]) -> float:
        return sum(1 for r in rows if not _green(r)) / len(rows)

    time_ratio = median(beside) / median(alone)
    fail_alone, fail_beside = failing(alone), failing(beside)
    ok = time_ratio <= LIMIT and fail_beside <= fail_alone * LIMIT
    return "\n".join(
        [
            f"land run alone: median {median(alone):.0f} s, failing {fail_alone:.0%} of {len(alone)}",
            f"beside two scoped checks: median {median(beside):.0f} s, failing {fail_beside:.0%} of {len(beside)}",
            f"time x{time_ratio:.2f} (limit x{LIMIT:.2f})",
            "max_concurrent_verify: 2 is supported" if ok else "keep max_concurrent_verify at 1",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("mode", choices=("dist", "lanes"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default=".")
    parser.add_argument("--temp-base", default=tempfile.gettempdir(), help="where run temp roots go")
    parser.add_argument("--scoped", nargs="*", default=SCOPED, help="the files a scoped check runs")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = Path(args.temp_base)
    summary = dist(args.runs, out, base) if args.mode == "dist" else lanes(args.runs, out, base, args.scoped)
    print(summary)
    (out / f"lane-bench-{args.mode}.txt").write_text(summary + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
