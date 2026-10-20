"""Native process checks for the Windows harness mutation guard."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_cli import sessions, win_lock
from agent_cli.core import AgentError
from agent_cli.dispatch.guards import area_guard
from agent_cli.dispatch.verification_evidence import Binding
from agent_cli.dispatch.verification_store import Store
from agent_cli.peers import take
from agent_cli.win_lock import exclusive, open_lock_file

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows file locking")


def test_guard_excludes_second_process_then_releases_after_crash(tmp_path: Path) -> None:
    package = Path(__file__).resolve().parents[1] / "src"
    ready = tmp_path / "holder-ready"
    code = (
        "import os,sys\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from pathlib import Path\n"
        "from agent_cli.dispatch.guards import area_guard\n"
        "with area_guard(Path(sys.argv[2]),'repo:alpha'):\n"
        " Path(sys.argv[3]).write_text('ready')\n"
        " sys.stdin.readline()\n"
        " os._exit(7)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(package), str(tmp_path), str(ready)],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), (
            holder.stderr.read() if holder.poll() is not None and holder.stderr else "holder timed out"
        )
        with pytest.raises(AgentError, match="busy"):
            with area_guard(tmp_path, "repo:alpha", timeout=0.05):
                pass
        with area_guard(tmp_path, "repo:beta", timeout=0.05):
            pass
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=5) == 7
        with area_guard(tmp_path, "repo:alpha", timeout=0.5):
            pass
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_guard_rejects_reparse_directory(tmp_path: Path) -> None:
    agent = tmp_path / ".agent"
    target = tmp_path / "target"
    target.mkdir()
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(agent), str(target)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    with pytest.raises(AgentError, match="reparse"):
        with area_guard(tmp_path, "repo:alpha"):
            pass


def test_verifier_store_rejects_reparse_directory(tmp_path: Path) -> None:
    agent = tmp_path / ".agent"
    agent.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    dispatches = agent / "dispatches"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(dispatches), str(target)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    binding = Binding("001-01", 1, "a" * 40, "b" * 64, "c" * 32)
    with pytest.raises(AgentError, match="reparse"):
        Store(tmp_path, binding, create=True)


def test_concurrent_peer_senders_and_drain_keep_every_message(tmp_path: Path) -> None:
    package = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from pathlib import Path\n"
        "from agent_cli.peers import send\n"
        "for number in range(40):\n"
        " send(Path(sys.argv[2]),to='recipient',text=f'{sys.argv[3]}-{number}',frm=sys.argv[3])\n"
    )
    senders = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", code, str(package), str(tmp_path), sender],
            stderr=subprocess.PIPE,
            text=True,
        )
        for sender in ("one", "two")
    ]
    try:
        rows = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rows.extend(take(tmp_path, "recipient"))
            if all(process.poll() is not None for process in senders):
                rows.extend(take(tmp_path, "recipient"))
                break
            time.sleep(0.01)
        for process in senders:
            assert process.wait(timeout=5) == 0, process.stderr.read() if process.stderr else ""
        assert {row["text"] for row in rows} == {
            f"{sender}-{number}" for sender in ("one", "two") for number in range(40)
        }
        assert len(rows) == 80
    finally:
        for process in senders:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_session_record_lock_excludes_second_process(tmp_path: Path) -> None:
    package = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from pathlib import Path\n"
        "from agent_cli.win_lock import exclusive,open_lock_file\n"
        "fd=open_lock_file(Path(sys.argv[2]),('.agent','sessions','s.lock'))\n"
        "try:\n"
        " with exclusive(fd,timeout=.05): print('acquired')\n"
        "except TimeoutError: print('busy')\n"
    )
    with sessions._locked(tmp_path, "s"):
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(package), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "busy"
    with sessions._locked(tmp_path, "s"):
        pass


def test_removal_holds_session_lock_through_by_pid_cleanup(tmp_path: Path, monkeypatch) -> None:
    sessions.set_record(tmp_path, "s", pid=123456)
    sessions.write_by_pid(tmp_path, 123456, "s")
    real = sessions._by_pid_locked
    checked = []

    def probe() -> str:
        fd = open_lock_file(tmp_path, (".agent", "sessions", "s.lock"))
        try:
            try:
                with exclusive(fd, timeout=0):
                    return "acquired"
            except TimeoutError:
                return "busy"
        finally:
            os.close(fd)

    @contextmanager
    def watch(root: Path):
        with ThreadPoolExecutor(1) as pool:
            checked.append(pool.submit(probe).result(timeout=2))
        with real(root):
            yield

    monkeypatch.setattr(sessions, "_by_pid_locked", watch)
    assert sessions.remove(tmp_path, "s") == ["123456"]
    assert checked == ["busy"]


def test_regular_file_open_outlasts_a_concurrent_replace(tmp_path: Path) -> None:
    target = tmp_path / "lifecycle.json"
    target.write_bytes(b"0")
    stop = threading.Event()
    replaced = [0]

    def writer() -> None:
        number = 0
        while not stop.is_set():
            number += 1
            temporary = tmp_path / f"tmp-{number}"
            temporary.write_bytes(str(number).encode())
            try:
                os.replace(temporary, target)
                replaced[0] += 1
            except PermissionError:  # this reader holds the file; the writer side is not under test
                temporary.unlink()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        before = replaced[0]
        for _ in range(2000):
            os.close(win_lock.open_regular_file(target))
        during = replaced[0] - before
    finally:
        stop.set()
        thread.join(timeout=5)
    assert during > 0, "no replacement landed while the reads ran"


@pytest.mark.parametrize("swap", ["reparse", "directory"])
def test_regular_file_open_refuses_an_unsafe_swap_without_retrying(tmp_path: Path, monkeypatch, swap) -> None:
    target = tmp_path / "lifecycle.json"
    target.write_bytes(b"{}")
    real_open, real_fstat = os.open, os.fstat
    opens = []

    def counted_open(path, flags, *args):
        opens.append(path)
        return real_open(path, flags, *args)

    monkeypatch.setattr(win_lock.os, "open", counted_open)
    if swap == "reparse":
        checks = iter([False, True])  # `lstat` before the open is clean, after it a reparse point
        monkeypatch.setattr(win_lock, "is_reparse", lambda info: next(checks))
    else:

        def directory_fstat(fd):
            info = list(real_fstat(fd))
            info[0] = stat.S_IFDIR | 0o700
            return os.stat_result(info)

        monkeypatch.setattr(win_lock.os, "fstat", directory_fstat)
    with pytest.raises(AgentError, match="regular file") as caught:
        win_lock.open_regular_file(target)
    assert type(caught.value) is AgentError
    assert len(opens) == 1


def test_regular_file_open_retries_permission_errors_then_raises(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "lifecycle.json"
    target.write_bytes(b"{}")
    real_open = os.open
    calls = []
    busy = win_lock.OPEN_ATTEMPTS - 1

    def busy_open(path, flags, *args):
        calls.append(path)
        if len(calls) <= busy:
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(win_lock.os, "open", busy_open)
    os.close(win_lock.open_regular_file(target))
    assert len(calls) == win_lock.OPEN_ATTEMPTS
    calls.clear()
    busy = win_lock.OPEN_ATTEMPTS
    with pytest.raises(PermissionError):
        win_lock.open_regular_file(target)
    assert len(calls) == win_lock.OPEN_ATTEMPTS


def test_session_lock_timeout_has_cli_error(tmp_path: Path, monkeypatch) -> None:
    class Busy:
        def __enter__(self):
            raise TimeoutError("held")

    monkeypatch.setattr(win_lock, "exclusive", lambda fd: Busy())
    for lock in (sessions._locked(tmp_path, "s"), sessions._by_pid_locked(tmp_path)):
        with pytest.raises(AgentError, match="session mutation lock is busy"):
            with lock:
                pass
