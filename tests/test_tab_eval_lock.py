"""Every Obsidian eval runs under the
drainer's eval lock, how each one ended is in `.agent/tabs/metrics.jsonl`, and doctor reports
the failure rate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_cli import obsidian_run, obsidian_tabs, sessions, tab_drain, tab_reset
from agent_cli.doctor import checks_state


def metrics(root: Path) -> list[dict[str, Any]]:
    path = root / tab_drain.TABS_DIR / "metrics.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def obsidian(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.CompletedProcess[str] | Exception | None]:
    """Scripted replies of `obsidian_run.run`, one per eval, with a CLI on PATH."""
    replies: list[subprocess.CompletedProcess[str] | Exception | None] = []

    def run(argv: list[str], timeout: float, **kw: Any) -> subprocess.CompletedProcess[str] | None:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(obsidian_run, "run", run)
    monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")
    return replies


def done(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["obsidian"], code, stdout=stdout, stderr=stderr)


def test_a_failed_eval_records_its_exit_code_and_stderr(tmp_root: Path, obsidian):
    obsidian.append(done(3, stderr="Error: no vault named Agent\n"))
    run = tab_drain.run_eval(tmp_root, "1", 5, source="claim")
    assert run.failed and (run.ended, run.returncode) == ("exited", 3)
    [row] = metrics(tmp_root)
    assert row["source"] == "claim" and row["eval_failed"] is True
    assert (row["ended"], row["returncode"], row["stderr"]) == ("exited", 3, "Error: no vault named Agent")


def test_a_timeout_and_a_closed_app_are_told_apart(tmp_root: Path, obsidian):
    obsidian += [subprocess.TimeoutExpired(["obsidian"], 5), None, done(0, "ok")]
    assert tab_drain.run_eval(tmp_root, "1", 5, source="open").ended == "timeout"
    assert tab_drain.run_eval(tmp_root, "1", 5, source="open").ended == "not-running"
    ok = tab_drain.run_eval(tmp_root, "1", 5, source="open")
    assert (ok.stdout, ok.failed) == ("ok", False)
    assert [(r["ended"], r["eval_failed"]) for r in metrics(tmp_root)] == [
        ("timeout", True),
        ("not-running", True),
        ("exited", False),
    ]


def test_a_held_eval_lock_keeps_a_second_eval_from_running(tmp_root: Path, obsidian):
    with tab_drain.eval_lock(tmp_root, 1):
        run = tab_drain.run_eval(tmp_root, "1", 5, source="claim", wait=0.1)
    assert run.ended == "lock-busy" and obsidian == []  # nothing was evaluated
    assert metrics(tmp_root)[-1]["ended"] == "lock-busy"


def test_a_codex_claim_waits_on_the_eval_lock(tmp_root: Path, obsidian, monkeypatch):
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setattr(obsidian_tabs, "CLAIM_LOCK_WAIT_S", 0.1)
    with tab_drain.eval_lock(tmp_root, 1):
        assert not obsidian_tabs.claim_active("mgr-codex-x", "⚙️ mgr-codex-x")
    failure = json.loads((tmp_root / ".agent/tabs/claim-failures.jsonl").read_text().splitlines()[-1])
    assert failure["reason"] == "lock-busy"
    obsidian.append(done(0, "=> relabeled 1"))
    assert obsidian_tabs.claim_active("mgr-codex-x", "⚙️ mgr-codex-x")
    assert [r["source"] for r in metrics(tmp_root)] == ["claim", "claim"]


def test_dispatch_open_runs_under_the_eval_lock(tmp_root: Path, obsidian, monkeypatch):
    from agent_cli import ops

    base = tmp_root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    record = {"id": "001-01", "feature": 1, "state": "blocked", "session_id": "w", "worktree": str(tmp_root)}
    (base / "001-01.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(obsidian_run, "launch", lambda: True)
    monkeypatch.setattr(
        tab_drain,
        "eval_lock",
        lambda root, wait: tab_reset.exclusive(root, "eval.lock", time.monotonic() + 0.1),
    )
    with tab_reset.exclusive(tmp_root, tab_drain.EVAL_LOCK, time.monotonic() + 1):
        op = ops.dispatch_open(tmp_root, dispatch_id="001-01")
    assert op.data["opened"] is False and "lock-busy" in op.data["reason"]
    obsidian.append(done(0, "=> opened"))
    assert ops.dispatch_open(tmp_root, dispatch_id="001-01").data["opened"] is True
    assert [r["source"] for r in metrics(tmp_root)] == ["open", "open"]


def test_a_reset_eval_in_the_drainer_takes_the_lock_and_is_recorded(tmp_root: Path, obsidian, monkeypatch):
    monkeypatch.setattr(tab_drain, "drain_root", tmp_root)
    obsidian.append(done(1, stderr="TypeError: leaf.view is null"))
    assert tab_reset._run_eval("code", 2) is None
    row = metrics(tmp_root)[-1]
    assert (row["source"], row["returncode"], row["stderr"]) == ("reset", 1, "TypeError: leaf.view is null")


def test_the_drainer_batch_row_says_why_it_failed(tmp_root: Path, obsidian, monkeypatch):
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    monkeypatch.setattr(tab_drain, "EVAL_FAILURE_LIMIT", 1)
    monkeypatch.setattr(obsidian_run, "running", lambda: True)

    class Process:
        pid = 99999

    monkeypatch.setattr(tab_drain.subprocess, "Popen", lambda *a, **kw: Process())
    sessions.set_record(tmp_root, "s1", name="mgr-1", pid=os.getpid())
    tab_drain.request(tmp_root, "manager", "s1", "mgr-1", "⚙️ mgr-1")
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    obsidian.append(done(0, "no result line here"))
    tab_drain.run(tmp_root)
    row = metrics(tmp_root)[-1]
    assert row["source"] == "drain" and row["eval_failed"] is True and row["requested"] == 1
    assert (row["ended"], row["returncode"]) == ("no-result-line", 0)


def test_doctor_reports_the_failure_rate(tmp_root: Path):
    now = time.time()
    rows = [
        {"at": now - 60, "eval_failed": True, "ended": "timeout"},
        {"at": now - 50, "eval_failed": False, "ended": "exited"},
        {"at": now - 40, "eval_failed": True, "ended": "timeout"},
        {"at": now - 30, "eval_failed": True, "ended": "exited", "returncode": 1},
        {"at": now - 2 * 86400, "eval_failed": True},  # outside the day
        {"at": now - 20, "requested": 1},  # an old row without the field
    ]
    for row in rows:
        tab_drain.record_metric(tmp_root, row)
    rate = tab_drain.failure_rate(tmp_root)
    assert rate == {"evals": 4, "failed": 3, "ended": {"timeout": 2, "exited": 1}}
    [row] = checks_state.tab_paints(tmp_root)
    assert row["level"] == "warn" and row["detail"].startswith(
        "3/4 Obsidian evals failed in the last 24 h (75.0%)"
    )
    assert "2 timeout" in row["detail"]


# WIN-BOX's stdout: an installer of 1.6.5 loading app package 1.14.4.
BANNER = (
    "\r\n2026-10-16 21:31:42 Loading updated app package "
    "C:\\Users\\user\\AppData\\Roaming\\obsidian\\obsidian-1.14.4.asar\r\n"
    "Your Obsidian installer is out of date. Please download the latest installer which "
    "includes better CLI support: https://obsidian.md/download\n"
)


def test_a_banner_only_eval_is_no_result_line_and_names_the_installer(tmp_root: Path, obsidian, monkeypatch):
    monkeypatch.setattr(tab_drain, "drain_root", tmp_root)
    obsidian.append(done(0, BANNER))
    assert tab_drain._eval([{"key": "manager-a", "match": "a", "label": "✅ a", "generated": True}]) is None
    run = tab_drain.last_run
    assert run is not None and run.ended == "no-result-line" and run.installer_outdated
    assert run.diagnostic() == {
        "ended": "no-result-line",
        "returncode": 0,
        "stderr": "",
        "installer_outdated": True,
    }


def test_a_banner_then_a_timeout_keeps_the_banner(tmp_root: Path, obsidian):
    obsidian.append(subprocess.TimeoutExpired(["obsidian"], 10, output=BANNER.encode()))
    run = tab_drain.run_eval(tmp_root, "1", 10, source="claim")
    assert run.failed and run.ended == "timeout" and run.installer_outdated
    row = metrics(tmp_root)[-1]
    assert (row["ended"], row["installer_outdated"]) == ("timeout", True)


def test_a_banner_then_a_result_line_still_paints(tmp_root: Path, obsidian, monkeypatch):
    monkeypatch.setattr(tab_drain, "drain_root", tmp_root)
    obsidian.append(done(0, BANNER + "=> AGENT_TAB_RESULTS:manager-a\n"))
    entries = [{"key": "manager-a", "match": "a", "label": "✅ a", "generated": True}]
    assert tab_drain._eval(entries) == {"manager-a"}
    run = tab_drain.last_run
    assert run is not None and not run.failed and run.ended == "exited" and run.installer_outdated


def test_a_clean_eval_row_has_no_installer_field(tmp_root: Path, obsidian):
    obsidian.append(done(0, "=> AGENT_TAB_RESULTS:\n"))
    tab_drain.run_eval(tmp_root, "1", 5, source="claim")
    assert "installer_outdated" not in metrics(tmp_root)[-1]
    assert not tab_drain.installer_outdated(tmp_root)


def test_doctor_names_the_installer_remedy(tmp_root: Path):
    now = time.time()
    tab_drain.record_metric(
        tmp_root, {"at": now - 2 * 86400, "eval_failed": True, "installer_outdated": True}
    )
    tab_drain.record_metric(tmp_root, {"at": now - 10, "eval_failed": False, "ended": "exited"})
    [row] = checks_state.tab_paints(tmp_root)
    assert row["level"] == "ok" and "installer" not in row["detail"]  # only outside the day
    tab_drain.record_metric(
        tmp_root, {"at": now - 5, "eval_failed": True, "ended": "timeout", "installer_outdated": True}
    )
    assert tab_drain.failure_rate(tmp_root) == {"evals": 2, "failed": 1, "ended": {"timeout": 1}}
    [row] = checks_state.tab_paints(tmp_root)
    assert row["level"] == "warn"
    assert row["detail"].endswith("update the Obsidian installer from https://obsidian.md/download")


def test_a_timed_out_cli_call_keeps_what_it_printed(monkeypatch):
    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    script = "import sys, time; sys.stdout.write('Your Obsidian installer is out of date'); sys.stdout.flush(); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        obsidian_run.run([sys.executable, "-c", script], 2)
    assert caught.value.output == b"Your Obsidian installer is out of date"


def _tab(i: int) -> dict[str, Any]:
    name = f"075 obsidian-json-cras · cl a3bb{i:04d}"
    return {
        "key": f"manager-01a14c02-a4df-70c1-90c1-d6fcfde1{i:04d}",
        "match": name,
        "token": f" · cl a3bb{i:04d}",
        "generated_name": f"mgr-075-obsidian-json-crash-{i}",
        "label": f"⚙️ {name}",
        "generated": True,
    }


def test_a_large_batch_is_painted_in_arguments_the_cli_sends_whole(monkeypatch):
    """Sixteen tabs made one 5.2 KB eval argument, past the 4 KB the Windows CLI converts (see
    obsidian_run)."""
    sent: list[list[str]] = []

    def run(argv: list[str], timeout: float, **kw: Any) -> subprocess.CompletedProcess[str]:
        sent.append(argv[1:])
        keys = [
            e["key"]
            for e in json.loads(argv[2].split("const entries = ", 1)[1].split("; const leaves", 1)[0])
        ]
        return done(0, "AGENT_TAB_RESULTS:" + ",".join(keys))

    monkeypatch.setattr(obsidian_run, "run", run)
    monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")
    tabs = [_tab(i) for i in range(16)]
    assert tab_drain._eval(tabs) == {t["key"] for t in tabs}
    assert len(sent) > 1 and all(obsidian_run.args_fit(args) for args in sent)


def test_a_small_batch_is_still_one_eval(obsidian):
    keys = [_tab(i)["key"] for i in range(4)]
    obsidian += [done(0, "AGENT_TAB_RESULTS:" + ",".join(keys)), RuntimeError("a second eval")]
    assert tab_drain._eval([_tab(i) for i in range(4)]) == set(keys)


def test_one_failed_request_fails_the_whole_batch(obsidian):
    obsidian += [done(0, "AGENT_TAB_RESULTS:x"), subprocess.TimeoutExpired(["obsidian"], 10)]
    assert tab_drain._eval([_tab(i) for i in range(16)]) is None
    assert tab_drain.last_run is not None and tab_drain.last_run.ended == "timeout"


@pytest.mark.parametrize("code", ["x" * obsidian_run.ARG_LIMIT, "⚙" * (obsidian_run.ARG_LIMIT // 3)])
def test_an_argument_the_cli_would_drop_is_never_sent(monkeypatch, code):
    monkeypatch.setattr(obsidian_run, "running", lambda: pytest.fail("checked the app for a refused request"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("sent an argument the CLI would drop"))
    with pytest.raises(obsidian_run.ArgumentTooLong):
        obsidian_run.run(["obsidian", "eval", f"code={code}"], 5)
    run = tab_drain.run_eval(None, code, 5, source="claim")
    assert run.ended == "oserror" and "ArgumentTooLong" in run.stderr


def test_arguments_within_the_limit_still_run(monkeypatch):
    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    plain, quotes = "x" * (obsidian_run.ARG_LIMIT - 1), '"' * (obsidian_run.ARG_LIMIT - 1)
    result = obsidian_run.run([sys.executable, "-c", "pass", plain, quotes], 30)
    assert result is not None and result.returncode == 0


def test_doctor_is_quiet_with_no_evals_and_ok_under_the_threshold(tmp_root: Path):
    assert checks_state.tab_paints(tmp_root) == []
    for i in range(100):
        tab_drain.record_metric(tmp_root, {"at": time.time(), "eval_failed": i == 0})
    [row] = checks_state.tab_paints(tmp_root)
    assert row["level"] == "ok" and row["detail"].startswith("1/100")
