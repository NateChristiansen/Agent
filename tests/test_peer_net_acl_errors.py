"""The Windows ACL check: a complete answer or a refusal, a detail that names the step, file and
a fixed reason but never a local path, and one shared deadline
`system (acl)` refused live sends on 2026-10-04 under load, with no way to tell why)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from agent_cli.peer_net import ack, system
from agent_cli.peer_net.errors import PeerNetError


class _Done:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _runner(monkeypatch, *outcomes):
    """subprocess.run returning (or raising) each outcome in turn; records the calls."""
    calls = []
    queue = list(outcomes)

    def run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(system.subprocess, "run", run)
    return calls


FACTS = "OWNER S-1-5-21-1-2-3-1001\nSID S-1-5-21-1-2-3-1001\nSID S-1-5-18\nEND\n"


def test_one_powershell_call_gives_owner_and_grants(monkeypatch):
    calls = _runner(monkeypatch, _Done(0, FACTS))
    owner, sids = system.acl_facts(Path("C:/x/state.db"))
    assert owner == "S-1-5-21-1-2-3-1001" and sids == {"S-1-5-21-1-2-3-1001", "S-1-5-18"}
    assert len(calls) == 1 and calls[0]["env"]["PEERNET_PATH"] == str(Path("C:/x/state.db"))


def test_the_script_stops_on_any_error_and_prints_only_after_collecting():
    script = system.ACL_SCRIPT
    assert script.startswith("$ErrorActionPreference = 'Stop'")
    assert script.index("$grants =") < script.index("'OWNER '")  # everything collected first


@pytest.mark.parametrize(
    "stdout",
    [
        "OWNER S-1-5-21-1\n",  # stopped before the grants: no END
        "OWNER S-1-5-21-1\nSID S-1-5-18\n",  # no END
        "SID S-1-5-18\nEND\n",  # no owner
        "OWNER S-1-5-21-1\nOWNER S-1-5-18\nEND\n",  # two owners
        "OWNER S-1-5-21-1\nBUILTIN\\Users\nEND\n",  # an unexpected line
        "OWNER not-a-sid\nEND\n",
        "",
    ],
)
def test_a_partial_or_malformed_answer_is_never_taken_as_privacy(monkeypatch, stdout):
    _runner(monkeypatch, _Done(0, stdout))
    with pytest.raises(PeerNetError) as caught:
        system.acl_facts(Path("state.db"))
    assert caught.value.detail == "acl state.db: bad-answer"


def test_a_single_slow_start_is_retried(monkeypatch):
    calls = _runner(monkeypatch, subprocess.TimeoutExpired("powershell", 30), _Done(0, FACTS))
    assert system.acl_facts(Path("state.db"))[0] == "S-1-5-21-1-2-3-1001" and len(calls) == 2


def test_a_timeout_twice_is_a_timeout(monkeypatch):
    _runner(monkeypatch, *[subprocess.TimeoutExpired("powershell", 30)] * 2)
    with pytest.raises(PeerNetError) as caught:
        system.acl_facts(Path("state.db"))
    assert (caught.value.code, caught.value.detail) == ("system", "acl state.db: timeout")


@pytest.mark.parametrize(
    "stderr",
    [
        "Get-Acl : Cannot find path 'C:\\Users\\user\\Projects\\Agent\\.agent\\peer-net\\state.db'",
        "Get-Acl : Access to /private/secret/Agent/state.db is denied\x07\x1b[31m",
    ],
)
def test_a_failing_tool_reports_a_code_never_its_text(monkeypatch, stderr):
    _runner(monkeypatch, _Done(1, "", stderr))
    with pytest.raises(PeerNetError) as caught:
        system.acl_facts(Path("C:/Users/user/Projects/Agent/.agent/peer-net/state.db"))
    detail = caught.value.detail
    assert detail == "acl state.db: exit-1"
    assert "user" not in detail and "secret" not in detail and len(detail) <= 64


def test_the_detail_is_a_valid_refusal_detail_on_the_wire(monkeypatch):
    _runner(monkeypatch, _Done(3, "", "x" * 500))
    with pytest.raises(PeerNetError) as caught:
        system.acl_facts(Path("a-very-long-file-name-that-keeps-going-and-going.db"))
    answer = {"v": 1, "ok": False, "error": caught.value.code, "detail": caught.value.detail}
    assert ack.refusal(answer) == "system"


def test_a_refused_launch_says_start_failed(monkeypatch):
    _runner(monkeypatch, PermissionError("sandbox"))
    with pytest.raises(PeerNetError) as caught:
        system.acl_facts(Path("state.db"))
    assert caught.value.detail == "acl state.db: start-failed"


def test_icacls_failures_name_the_file_with_a_code(monkeypatch):
    _runner(monkeypatch, _Done(5, "C:\\Users\\user\\x: Access is denied.\n", ""))
    with pytest.raises(PeerNetError) as caught:
        system._icacls(Path("C:/x/accept.cmd"), "*S-1-5-18:F")
    assert caught.value.detail == "icacls accept.cmd: exit-5"


# --- one deadline -------------------------------------------------------------------------------


def test_the_deadline_caps_each_tool_timeout(monkeypatch):
    calls = _runner(monkeypatch, _Done(0, FACTS))
    with system.deadline(time.monotonic() + 2.0):
        system.acl_facts(Path("state.db"))
    assert calls[0]["timeout"] <= 2.0


def test_no_retry_starts_without_budget(monkeypatch):
    def slow(command, **kwargs):
        time.sleep(min(kwargs["timeout"], 5))
        raise subprocess.TimeoutExpired("powershell", kwargs["timeout"])

    monkeypatch.setattr(system.subprocess, "run", slow)
    started = time.monotonic()
    with pytest.raises(PeerNetError), system.deadline(time.monotonic() + 1.2):
        system.acl_facts(Path("state.db"))
    assert time.monotonic() - started < 1.6  # one try; the retry would have exceeded the budget


def test_an_expired_deadline_refuses_before_any_tool_runs(monkeypatch):
    calls = _runner(monkeypatch)
    with pytest.raises(PeerNetError) as caught, system.deadline(time.monotonic() - 1):
        system.acl_facts(Path("state.db"))
    assert caught.value.code == "timeout" and calls == []


def test_whoami_shares_the_deadline(monkeypatch):
    calls = _runner(monkeypatch, _Done(0, '"host\\me","S-1-5-21-9"\n'))
    with system.deadline(time.monotonic() + 0.7):
        system._whoami("/user")
    assert calls[0]["timeout"] <= 0.7
