"""The tab drainer proves itself by a token, not a pid.

A Windows venv python.exe is a launcher that starts the real interpreter as its child, so the
pid Popen returned never equalled the drainer's os.getpid() and every drainer exited on its
first check. Tabs never followed status on Windows."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_cli import tab_drain


def _enable(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    argvs: list[list[str]] = []

    class Launcher:  # the venv launcher: its pid is not the drainer's
        pid = 424242

    def spawn(argv, root):
        argvs.append(argv)
        return Launcher()

    monkeypatch.setattr(tab_drain, "_spawn", spawn)
    return argvs


def test_a_drainer_started_through_a_launcher_claims_the_run(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    argvs = _enable(monkeypatch)
    tab_drain.request(tmp_root, "watch", "001-01", "001-01 watch", "✳ 001-01 watch", generated=False)
    token = argvs[0][-1]
    state = tab_drain._state(tmp_root)
    assert state["runner"] == 424242 and state["runner_token"] == token
    monkeypatch.setattr(tab_drain, "_eval", lambda entries: {e["key"] for e in entries})
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    tab_drain.run(tmp_root, token)  # os.getpid() here is not 424242, as on Windows
    state = tab_drain._state(tmp_root)
    assert state["painted"]["watch-001-01"]["label"] == "✳ 001-01 watch"
    assert state["runner"] is None and state["runner_token"] is None


def test_a_stale_token_does_not_run(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    _enable(monkeypatch)
    tab_drain.request(tmp_root, "watch", "001-01", "001-01 watch", "✳ 001-01 watch", generated=False)
    monkeypatch.setattr(tab_drain, "_eval", lambda entries: pytest.fail("a superseded drainer must not eval"))
    tab_drain.run(tmp_root, "not-the-current-token")


def test_the_drainer_records_its_own_pid_once_it_claims(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    argvs = _enable(monkeypatch)
    tab_drain.request(tmp_root, "watch", "001-01", "001-01 watch", "✳ 001-01 watch", generated=False)
    seen: list[int | None] = []
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)

    def fake_eval(entries):
        seen.append(tab_drain._state(tmp_root)["runner"])
        return set()

    monkeypatch.setattr(tab_drain, "_eval", fake_eval)
    tab_drain.run(tmp_root, argvs[0][-1])
    assert seen and seen[0] == os.getpid()  # a later request checks this process, not the launcher


def test_windows_spawn_breaks_away_then_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A hook's job may forbid breakaway; the drainer then starts without it. Both starts use
    CREATE_NO_WINDOW, never DETACHED_PROCESS: with it the owner saw terminal windows open on
    every turn while tabs were on (#55)."""
    monkeypatch.setattr(tab_drain.sys, "platform", "win32")
    for name, value in (
        ("CREATE_NO_WINDOW", 0x8000000),
        ("CREATE_NEW_PROCESS_GROUP", 0x200),
        ("CREATE_BREAKAWAY_FROM_JOB", 0x1000000),
    ):
        monkeypatch.setattr(subprocess, name, value, raising=False)
    flags: list[int] = []

    def popen(argv, creationflags=0, **kw):
        flags.append(creationflags)
        if creationflags & 0x1000000:
            raise PermissionError("breakaway not allowed")
        return object()

    monkeypatch.setattr(tab_drain.subprocess, "Popen", popen)
    tab_drain._spawn(["python", "-m", "agent_cli.tab_drain"], tmp_path)
    assert flags == [0x8000000 | 0x200 | 0x1000000, 0x8000000 | 0x200]
