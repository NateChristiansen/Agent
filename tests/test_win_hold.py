"""The Windows branch of the board and land locks, run on POSIX through a flock stand-in for
msvcrt. `win_lock.exclusive` defaults to 30 s; a blocking lock must not inherit that."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager

import pytest

from agent_cli import land_journal, win_hold, win_lock
from agent_cli.core import AgentError
from agent_cli.dispatch import write_scope

try:
    import fcntl
except ImportError:  # the native Windows run is covered by test_win_lock
    fcntl = None

pytestmark = pytest.mark.skipif(fcntl is None, reason="needs flock as the msvcrt stand-in")


REAL_FLOCK = fcntl.flock if fcntl is not None else None


class Recorder:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    @contextmanager
    def exclusive(self, fd, *, timeout=30):
        assert fcntl is not None and REAL_FLOCK is not None
        self.timeouts.append(timeout)
        deadline = time.monotonic() + timeout
        while True:
            try:
                REAL_FLOCK(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Windows mutation lock is busy") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            REAL_FLOCK(fd, fcntl.LOCK_UN)


@pytest.fixture
def windows(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(win_hold, "windows", lambda: True)
    monkeypatch.setattr(win_lock, "exclusive", rec.exclusive)

    def no_flock(*args, **kwargs):
        raise AssertionError("the Windows branch must not call fcntl.flock")

    monkeypatch.setattr(fcntl, "flock", no_flock)
    return rec


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(win_hold, "windows", lambda: False)

    def no_exclusive(*args, **kwargs):
        raise AssertionError("the POSIX branch must not call win_lock")

    monkeypatch.setattr(win_lock, "exclusive", no_exclusive)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(write_scope, "board_dir", lambda root: tmp_path / "board")
    return lambda: write_scope.board_lock(tmp_path)


@pytest.fixture
def op(tmp_path):
    def run():
        land_journal.op_update(tmp_path, "op1", state="queued")
        assert json.loads(land_journal.op_path(tmp_path, "op1").read_text())["state"] == "queued"

    return run


BLOCKING = ["board"]


@pytest.mark.parametrize("name", BLOCKING)
def test_blocking_lock_waits_without_timeout_on_windows(windows, name, request):
    with request.getfixturevalue(name)():
        pass
    assert windows.timeouts == [math.inf]


def test_op_lock_waits_without_timeout_on_windows(windows, op):
    op()
    assert windows.timeouts == [math.inf]


def test_land_lock_keeps_its_wait_on_windows(windows, tmp_path):
    with land_journal.flock(tmp_path, wait_s=7):
        pass
    with land_journal.flock(tmp_path):
        pass
    assert windows.timeouts == [7, 0]


@pytest.mark.parametrize("name", BLOCKING)
def test_blocking_lock_uses_flock_on_posix(posix, name, request):
    with request.getfixturevalue(name)():
        pass


def test_op_and_land_lock_use_flock_on_posix(posix, op, tmp_path):
    op()
    with land_journal.flock(tmp_path):
        pass


@pytest.mark.parametrize("name", BLOCKING)
def test_second_holder_waits_for_blocking_lock(windows, name, request):
    make = request.getfixturevalue(name)
    entered = threading.Event()
    got = threading.Event()

    def second():
        entered.set()
        with make():
            got.set()

    with make():
        thread = threading.Thread(target=second)
        thread.start()
        entered.wait(2)
        assert not got.wait(0.2)
    thread.join(2)
    assert got.is_set()
    assert windows.timeouts == [math.inf, math.inf]


def test_second_holder_waits_for_op_lock(windows, op, tmp_path):
    path = land_journal.op_path(tmp_path, "op1").with_suffix(".lock")
    land_journal.land_dir(tmp_path).mkdir(parents=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    done = threading.Event()
    thread = threading.Thread(target=lambda: (op(), done.set()))
    with windows.exclusive(fd):
        thread.start()
        assert not done.wait(0.2)
    thread.join(2)
    assert done.is_set()
    os.close(fd)


def test_land_lock_refuses_second_holder_with_same_text(windows, tmp_path):
    with land_journal.flock(tmp_path):
        with pytest.raises(AgentError) as quick, land_journal.flock(tmp_path):
            pass
        started = time.monotonic()
        with pytest.raises(AgentError) as slow, land_journal.flock(tmp_path, wait_s=0.2):
            pass
    assert str(quick.value) == "another landing or recovery holds the land lock"
    assert str(slow.value) == "another landing or recovery holds the land lock after waiting 0.2 s"
    assert time.monotonic() - started >= 0.2
    with land_journal.flock(tmp_path):
        pass


def test_land_lock_text_matches_on_posix(posix, tmp_path, monkeypatch):
    monkeypatch.setattr(land_journal, "LOCK_POLL_S", 0.01)
    with land_journal.flock(tmp_path):
        with pytest.raises(AgentError) as slow, land_journal.flock(tmp_path, wait_s=0.1):
            pass
    assert str(slow.value) == "another landing or recovery holds the land lock after waiting 0.1 s"
