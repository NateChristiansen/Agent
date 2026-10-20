"""Windows inventory keeps a live PID when its environment cannot be read."""

from __future__ import annotations

import types

import pytest

from agent_cli import procs, win_snapshot, winproc


@pytest.mark.parametrize("failures", [1, 2])
def test_environ_failure_keeps_process_row(monkeypatch: pytest.MonkeyPatch, failures: int):
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    class Process:
        def __init__(self):
            self.reads = 0

        def cmdline(self):
            return ["python", "worker.py"]

        def create_time(self):
            return 1.0

        def environ(self):
            self.reads += 1
            if self.reads <= failures:
                raise NoSuchProcess()
            return {procs.TAG_DISPATCH: "ours"}

    process = Process()

    fake = types.SimpleNamespace(
        Process=lambda pid: process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied
    )
    monkeypatch.setattr(win_snapshot.importlib, "import_module", lambda name: fake)
    monkeypatch.setattr(win_snapshot.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(winproc, "pid_started", lambda pid: "birth", raising=False)
    monkeypatch.setattr(winproc, "process_entries", lambda: [(2252, 728, "python.exe")], raising=False)

    rows = win_snapshot.snapshot()

    assert len(rows) == 1 and rows[0].pid == 2252 and rows[0].started == "birth"
    assert rows[0].dispatch == ("ours" if failures == 1 else "")
    assert rows[0].tag_error == ("" if failures == 1 else "NoSuchProcess")


def test_access_denied_is_not_retried(monkeypatch: pytest.MonkeyPatch):
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    reads = []
    process = types.SimpleNamespace(
        cmdline=lambda: [],
        create_time=lambda: 1.0,
        environ=lambda: reads.append(1) or (_ for _ in ()).throw(AccessDenied()),
    )
    fake = types.SimpleNamespace(
        Process=lambda pid: process,
        NoSuchProcess=NoSuchProcess,
        AccessDenied=AccessDenied,
    )
    monkeypatch.setattr(win_snapshot.importlib, "import_module", lambda name: fake)
    monkeypatch.setattr(winproc, "pid_started", lambda pid: "birth", raising=False)
    monkeypatch.setattr(winproc, "process_entries", lambda: [(2252, 728, "python.exe")], raising=False)

    rows = win_snapshot.snapshot()

    assert reads == [1]
    assert rows[0].tag_error == "AccessDenied"


def test_non_string_environment_value_reads_as_untagged(monkeypatch: pytest.MonkeyPatch):
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    process = types.SimpleNamespace(
        cmdline=lambda: ["python"],
        create_time=lambda: 1.0,
        environ=lambda: {procs.TAG_DISPATCH: 7, procs.TAG_SESSION: "s1"},
    )
    fake = types.SimpleNamespace(
        Process=lambda pid: process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied
    )
    monkeypatch.setattr(win_snapshot.importlib, "import_module", lambda name: fake)
    monkeypatch.setattr(winproc, "pid_started", lambda pid: "birth", raising=False)
    monkeypatch.setattr(winproc, "process_entries", lambda: [(2252, 728, "python.exe")], raising=False)

    (row,) = win_snapshot.snapshot()

    assert (row.dispatch, row.session, row.tag_error) == ("", "s1", "")


def test_cmdline_open_failure_cannot_drop_toolhelp_row(monkeypatch: pytest.MonkeyPatch):
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    process = types.SimpleNamespace(
        cmdline=lambda: (_ for _ in ()).throw(NoSuchProcess()),
        create_time=lambda: 1.0,
        environ=lambda: {procs.TAG_DISPATCH: "ours"},
    )
    fake = types.SimpleNamespace(
        Process=lambda pid: process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied
    )
    monkeypatch.setattr(win_snapshot.importlib, "import_module", lambda name: fake)
    monkeypatch.setattr(winproc, "pid_started", lambda pid: "birth", raising=False)
    monkeypatch.setattr(winproc, "process_entries", lambda: [(2252, 728, "python.exe")], raising=False)

    rows = win_snapshot.snapshot()

    assert [(p.pid, p.ppid, p.command, p.dispatch, p.started) for p in rows] == [
        (2252, 728, "python.exe", "ours", "birth")
    ]
