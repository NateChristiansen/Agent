"""Inventory churn: a process that starts between the POSIX snapshot's two `ps`
calls has its tags read again by pid. Fake `ps` output, no real processes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_cli import procs
from agent_cli.dispatch import allocation
from test_dispatch_alloc_live import REAL_SNAPSHOT

MANAGER, LATE, GONE = 10, 20, 30


def _fake_ps(monkeypatch: pytest.MonkeyPatch, root: Path, alive: set[int]) -> list[list[str]]:
    """`ps -E -A` saw only the manager; the command read also lists `LATE` and `GONE`; the re-read
    by pid finds `LATE`, tagged for dispatch 001-mac-01, and not `GONE`."""
    calls: list[list[str]] = []
    tagged = f"{LATE} python {procs.TAG_DISPATCH}=001-mac-01 {procs.TAG_ROOT}={root}\n"

    def ps(args: list[str]) -> str:
        calls.append(args)
        if "-E" not in args:
            return "".join(f"{pid} 1 {pid} 00:01 python\n" for pid in (MANAGER, LATE, GONE))
        return f"{MANAGER} python\n" if "-A" in args else tagged

    monkeypatch.setattr(procs, "_ps", ps)
    monkeypatch.setattr(procs, "_alive", lambda pid: pid in alive)
    monkeypatch.setattr(procs, "snapshot", REAL_SNAPSHOT)
    return calls


def test_a_late_process_is_read_by_pid_and_a_vanished_one_voids_nothing(tmp_path: Path, monkeypatch):
    calls = _fake_ps(monkeypatch, tmp_path, alive={MANAGER, LATE})
    if sys.platform == "win32":
        monkeypatch.setattr(procs.sys, "platform", "darwin")  # the POSIX `ps` snapshot is under test
        monkeypatch.setattr(os, "getpgrp", lambda: 1, raising=False)
    rows = {p.pid: p for p in procs.snapshot()}
    assert ["-p", f"{LATE},{GONE}"] == calls[2][-2:]
    assert rows[LATE].dispatch == "001-mac-01" and not rows[LATE].tag_error
    assert not rows[GONE].tag_error
    assert allocation.owned_pids(tmp_path, "001-mac-01") == ([LATE], True)


def test_a_live_process_whose_tags_stay_unread_voids_trust(tmp_path: Path, monkeypatch):
    _fake_ps(monkeypatch, tmp_path, alive={MANAGER, LATE, GONE})
    assert procs.snapshot()[2].tag_error
    assert allocation.owned_pids(tmp_path, "001-mac-02") == ([], False)
