"""Full-suite discovery with durable partial results, even when Windows pytest hangs.

Also loaded as a pytest plugin in the child. This report never turns a failure into a pass.
CI explicitly chooses whether its exit status gates the branch.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def record(event: dict[str, Any]) -> None:
    target = os.environ.get("AGENT_CI_EVENTS")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, default=str) + "\n")
            stream.flush()


def pytest_collection_finish(session: Any) -> None:
    record({"kind": "collection", "count": len(session.items), "items": [i.nodeid for i in session.items]})


def pytest_collectreport(report: Any) -> None:
    if report.failed or report.skipped:
        record(
            {
                "kind": "collect",
                "nodeid": report.nodeid,
                "outcome": report.outcome,
                "reason": str(report.longrepr),
            }
        )


def pytest_runtest_logreport(report: Any) -> None:
    record(
        {
            "kind": "test",
            "nodeid": report.nodeid,
            "phase": report.when,
            "outcome": report.outcome,
            "duration": report.duration,
            "reason": str(report.longrepr) if report.longrepr else "",
        }
    )


def inventory(events: list[dict[str, Any]], timed_out: bool, exit_code: int) -> dict[str, Any]:
    collected: list[str] = []
    tests: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for event in events:
        if event["kind"] == "collection":
            collected = event["items"]
        elif event["kind"] == "collect":
            issues.append({**event, "classification": "untriaged"})
        elif event["kind"] == "test":
            case = tests.setdefault(event["nodeid"], {"nodeid": event["nodeid"], "reports": []})
            case["reports"].append(event)
    counts = {"passed": 0, "failed": 0, "skipped": 0, "incomplete": 0}
    for nodeid in dict.fromkeys([*collected, *tests]):
        case = tests.get(nodeid, {"nodeid": nodeid, "reports": []})
        reports = case["reports"]
        failed = [r for r in reports if r["outcome"] == "failed"]
        skipped = [r for r in reports if r["outcome"] == "skipped"]
        complete = any(r["phase"] == "teardown" for r in reports)
        passed_call = any(r["phase"] == "call" and r["outcome"] == "passed" for r in reports)
        outcome = (
            "failed"
            if failed
            else "skipped"
            if skipped
            else "passed"
            if complete and passed_call
            else "incomplete"
        )
        counts[outcome] += 1
        if outcome != "passed":
            issues.append({**case, "outcome": outcome, "classification": "untriaged"})
    if timed_out or exit_code or not collected:
        issues.append(
            {
                "kind": "run",
                "outcome": "timeout" if timed_out else "nonzero-exit" if exit_code else "empty-collection",
                "exit_code": exit_code,
                "classification": "untriaged",
            }
        )
    return {
        "collected": len(collected),
        "counts": counts,
        "issues": issues,
        "promotion_ready": not issues and bool(collected),
        "timed_out": timed_out,
        "exit_code": exit_code,
    }


def stop_tree(process: subprocess.Popen[Any], timeout: float = 10) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=timeout)


def run(output: Path, deadline: float, args: list[str]) -> int:
    output.mkdir(parents=True, exist_ok=True)
    events_path = output / "events.jsonl"
    events_path.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "AGENT_"))}
    env.update(AGENT_CI_EVENTS=str(events_path.resolve()), AGENT_NOTIFY="off")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-n",
        "0",
        "-rA",
        "--continue-on-collection-errors",
        "-p",
        "agent_cli.ci_pytest",
        f"--junitxml={output.resolve() / 'junit.xml'}",
        *args,
    ]
    metadata = {
        "platform": platform.platform(),
        "python": sys.version,
        "command": command,
        "deadline_seconds": deadline,
        "pytest": importlib.metadata.version("pytest"),
        "revision": os.environ.get("GITHUB_SHA", "local"),
        "started_at": time.time(),
    }
    (output / "run.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    started = time.monotonic()
    timed_out = False
    interrupted = False
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != "nt"
        )
        try:
            code = process.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_tree(process)
            code = 124
        except KeyboardInterrupt:
            interrupted = True
            stop_tree(process)
            code = 130
    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append(
                {
                    "kind": "collect",
                    "nodeid": "event-stream",
                    "outcome": "failed",
                    "reason": "truncated event",
                }
            )
    summary = inventory(events, timed_out, code)
    result = {
        **metadata,
        **summary,
        "interrupted": interrupted,
        "duration_seconds": time.monotonic() - started,
    }
    if not (output / "junit.xml").exists():
        suite = ET.Element("testsuite", name="interrupted-discovery", tests=str(len(summary["issues"])))
        for issue in summary["issues"]:
            case = ET.SubElement(suite, "testcase", name=issue.get("nodeid", issue.get("kind", "run")))
            ET.SubElement(case, "error", message="partial result; see inventory.json").text = json.dumps(
                issue
            )
        ET.ElementTree(suite).write(output / "junit.xml", encoding="utf-8", xml_declaration=True)
    (output / "inventory.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: result[k] for k in ("collected", "counts", "timed_out", "exit_code", "promotion_ready")}
        )
    )
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=2400)
    options, args = parser.parse_known_args()
    if options.timeout <= 0:
        parser.error("timeout must be positive")
    return run(options.output, options.timeout, args)


if __name__ == "__main__":
    raise SystemExit(main())
