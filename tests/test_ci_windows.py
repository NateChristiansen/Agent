"""Discovery retains failures/timeouts; a missing guard denial is never a successful sample."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_cli import ci_launchers, ci_pytest


def test_system_python_probe_does_not_inherit_uv_activated_venv(monkeypatch, tmp_path):
    venv_bin = tmp_path / ".venv" / "bin"
    system_bin = tmp_path / "system-bin"
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), str(system_bin)]))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_bin.parent))
    monkeypatch.setenv("CODEX_SESSION_ID", "must-not-leak")
    env = ci_launchers.shell_environment(tmp_path)
    assert env["PATH"] == str(system_bin)
    assert "VIRTUAL_ENV" not in env and "CODEX_SESSION_ID" not in env


def test_inventory_retains_failure_skip_and_interrupted_test():
    events = [{"kind": "collection", "items": ["pass", "fail", "skip", "pending"]}]
    for nodeid, outcome in (("pass", "passed"), ("fail", "failed"), ("skip", "skipped")):
        events.extend(
            [
                {
                    "kind": "test",
                    "nodeid": nodeid,
                    "phase": "call",
                    "outcome": outcome,
                    "reason": "explicit reason",
                },
                {"kind": "test", "nodeid": nodeid, "phase": "teardown", "outcome": "passed"},
            ]
        )
    result = ci_pytest.inventory(events, True, 124)
    assert result["counts"] == {"passed": 1, "failed": 1, "skipped": 1, "incomplete": 1}
    assert not result["promotion_ready"]
    assert {i.get("nodeid") for i in result["issues"]} >= {"fail", "skip", "pending"}
    assert all(i["classification"] == "untriaged" for i in result["issues"])


def test_collection_error_and_empty_suite_do_not_promote():
    result = ci_pytest.inventory(
        [{"kind": "collect", "nodeid": "broken.py", "outcome": "failed", "reason": "syntax error"}], False, 2
    )
    assert result["issues"][0]["reason"] == "syntax error"
    assert not result["promotion_ready"]
    assert not ci_pytest.inventory([], False, 0)["promotion_ready"]


def test_real_discovery_retains_nonzero_exit_and_junit(tmp_path):
    test = tmp_path / "test_sample.py"
    test.write_text(
        "def test_pass():\n    pass\ndef test_fail():\n    assert False, 'preserved failure'\n",
        encoding="utf-8",
    )
    output = tmp_path / "results"
    assert ci_pytest.run(output, 30, [str(test)]) == 1
    result = json.loads((output / "inventory.json").read_text())
    assert result["counts"]["failed"] == result["counts"]["passed"] == 1
    assert "preserved failure" in json.dumps(result["issues"])
    assert (output / "junit.xml").is_file()
    assert (output / "events.jsonl").is_file()


def test_real_discovery_timeout_keeps_partial_inventory(tmp_path):
    test = tmp_path / "test_sleep.py"
    test.write_text("import time\ndef test_sleep():\n    time.sleep(60)\n", encoding="utf-8")
    output = tmp_path / "results"
    assert ci_pytest.run(output, 3, [str(test)]) == 124
    result = json.loads((output / "inventory.json").read_text())
    assert result["timed_out"] and not result["promotion_ready"]
    assert any(issue.get("outcome") == "timeout" for issue in result["issues"])
    assert (output / "junit.xml").is_file()
    assert (output / "pytest.log").is_file()


def test_real_discovery_runs_good_tests_despite_collection_errors(tmp_path):
    (tmp_path / "test_good.py").write_text("def test_good():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_broken.py").write_text(
        "raise ImportError('preserved collection failure')\n", encoding="utf-8"
    )
    output = tmp_path / "results"
    assert ci_pytest.run(output, 30, [str(tmp_path)]) != 0
    result = json.loads((output / "inventory.json").read_text())
    assert result["counts"]["passed"] == 1
    assert not result["promotion_ready"]
    assert any(
        issue.get("kind") == "collect" and "preserved collection failure" in issue["reason"]
        for issue in result["issues"]
    )


def test_discovery_parent_interrupt_preserves_inventory_and_reaps_child(tmp_path, monkeypatch):
    test = tmp_path / "test_sleep.py"
    test.write_text("import time\ndef test_sleep():\n    time.sleep(60)\n", encoding="utf-8")
    output = tmp_path / "results"
    original_wait = subprocess.Popen.wait
    children = []

    def interrupt_once(process, *args, **kwargs):
        if not children:
            children.append(process)
            raise KeyboardInterrupt
        return original_wait(process, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupt_once)
    assert ci_pytest.run(output, 30, [str(test)]) == 130
    assert children[0].poll() is not None
    result = json.loads((output / "inventory.json").read_text())
    assert result["interrupted"] and not result["promotion_ready"]
    assert result["exit_code"] == 130
    assert (output / "junit.xml").is_file()
    assert (output / "pytest.log").is_file()


@pytest.mark.parametrize("stdout", ["", "{}", '{"hookSpecificOutput":{"permissionDecision":"allow"}}'])
def test_empty_or_allow_output_is_not_a_working_guard(stdout):
    assert not ci_launchers.validates({"exit_code": 0, "stdout": stdout}, "guard")


def test_timing_failed_cold_probe_stops_without_success_statistics(monkeypatch, tmp_path):
    calls = []

    def failed(*args):
        calls.append(args)
        return {"exit_code": 0, "stdout": "", "ms": 1}

    monkeypatch.setattr(ci_launchers, "sample", failed)
    result = ci_launchers.measure(["unused"], tmp_path, {}, 30, 10, "guard")
    assert len(calls) == 1 and not result["valid"]
    assert "median_ms" not in result


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_shell_call_uses_resolved_executable(monkeypatch, shell):
    resolved = "C:/Program Files/Git/bin/bash.exe" if shell == "bash" else "C:/Windows/powershell.exe"
    monkeypatch.setattr(ci_launchers.shutil, "which", lambda _: resolved)
    assert ci_launchers.shell_call(shell, ["python", "-c", "pass"], None)[0] == resolved


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("body", ['{"exact":true}', '{"text":"雪 café"}'])
def test_native_shell_paths_spaces_quotes_and_stdin_are_preserved(shell, body, tmp_path):
    executable = "powershell.exe" if shell == "powershell" else "bash"
    if not ci_launchers.shutil.which(executable):
        pytest.skip(f"native {executable} is not available on this platform")
    payload = tmp_path / "payload with ' quote.json"
    payload.write_text(body, encoding="utf-8")
    code = "import sys; print(sys.argv[1]); print(sys.stdin.read().strip())"
    command = ci_launchers.shell_call(
        shell, [Path(sys.executable).as_posix(), "-c", code, "spaces and ' quotes"], payload
    )
    result = subprocess.run(
        command,
        input=body if shell == "powershell" else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert result.stdout.splitlines() == ["spaces and ' quotes", body]


def test_windows_inventory_jobs_are_explicitly_report_only_and_keep_artifacts():
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/tests.yml").read_text())["jobs"]
    assert not jobs["windows-setup"].get("continue-on-error")
    for name in ("windows-discovery", "windows-launchers"):
        job = jobs[name]
        assert job["continue-on-error"] is True and job["timeout-minutes"] == 60
        # manual only: they cost CI minutes and gate nothing
        assert job["if"] == "github.event_name == 'workflow_dispatch'"
        upload = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact"))
        assert upload["if"] == "always()"


def test_percentile_uses_nearest_rank_and_deadline_cannot_pass(tmp_path):
    assert ci_launchers.statistics_for(list(range(1, 31)))["p95_ms"] == 29
    result = ci_launchers.measure(["unused"], tmp_path, {}, 30, 1, "guard", deadline=0)
    assert not result["valid"] and result["reason"] == "matrix deadline exceeded"
