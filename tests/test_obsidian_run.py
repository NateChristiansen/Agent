"""The Obsidian CLI never starts Obsidian on a pipe (owner report, 2026-10-07).

With the app closed, `obsidian eval` is a full app launch that keeps the caller's stdio. The tab
drainer ran it with captured pipes on every retry: obsidian.log showed app starts 2 to 10 s
apart, and an instance whose pipe reader had gone raised `write EPIPE` from every console write
("A JavaScript error occurred in the main process"). Background paths now never call the CLI
against a closed app, and no call holds a pipe.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_cli import obsidian_run, sessions, tab_drain


@pytest.mark.skipif(
    os.name != "posix", reason="the CLI socket is a Unix socket; Windows asks the process list"
)
def test_the_cli_socket_says_whether_the_app_runs() -> None:
    folder = Path(tempfile.mkdtemp(prefix="obs", dir="/tmp"))  # AF_UNIX paths are short on macOS
    sock = folder / "cli.sock"
    assert obsidian_run.socket_answers(sock) is None  # no socket: ask the process list
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    server.listen(1)
    try:
        assert obsidian_run.socket_answers(sock) is True
    finally:
        server.close()
    assert obsidian_run.socket_answers(sock) is False  # a stale socket: the app is gone
    sock.unlink()
    folder.rmdir()


def test_a_closed_app_is_never_started_by_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obsidian_run, "running", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("the CLI ran against a closed app"))
    assert obsidian_run.run(["obsidian", "eval", "code=1"], 5) is None


def test_a_cli_call_holds_no_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obsidian_run, "running", lambda: True)
    seen: dict[str, Any] = {}
    real = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    script = "import sys; sys.stdout.write('x' * 200000); sys.stderr.write('warn')"
    result = obsidian_run.run([sys.executable, "-c", script], 30)
    assert result is not None and result.returncode == 0
    assert len(result.stdout) == 200000 and result.stderr == "warn"
    assert seen["stdin"] is subprocess.DEVNULL  # the drainer's stdin since it existed; see obsidian_run
    assert seen["stdout"] is not subprocess.PIPE and seen["stderr"] is not subprocess.PIPE
    assert seen["start_new_session"] is (os.name == "posix")


def test_the_drainer_stops_while_the_app_is_closed(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No eval at all, so no launch-and-kill loop; the request stays for the next drainer."""
    from test_tab_drain import _enable

    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    sid = "claude-1"
    sessions.set_record(tmp_root, sid, name="mgr-036-x")
    tab_drain.request(tmp_root, "manager", sid, "mgr-036-x", "✋ mgr-036-x")
    state = tab_drain._state(tmp_root)  # pyright: ignore[reportPrivateUsage]
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(obsidian_run, "running", lambda: False)
    monkeypatch.setattr(tab_drain, "_eval", lambda entries: pytest.fail("evaluated against a closed app"))
    tab_drain.run(tmp_root)
    assert tab_drain._state(tmp_root)["runner"] is None  # pyright: ignore[reportPrivateUsage]
    assert tab_drain._pending(tmp_root, tab_drain._state(tmp_root))  # pyright: ignore[reportPrivateUsage]


def test_an_explicit_command_starts_the_app_through_the_os_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cli import obsidian

    state = {"up": False}
    monkeypatch.setattr(obsidian, "cli_path", lambda: sys.executable)
    monkeypatch.setattr(obsidian_run, "running", lambda: state["up"])
    monkeypatch.setattr(obsidian_run, "launch", lambda: state.update(up=True) or True)
    assert (
        obsidian.run_cli(["-c", "print('ok')"], require_running=True)["skipped"] == "Obsidian is not running"
    )
    assert state["up"] is False  # a background call never launches
    result = obsidian.run_cli(["-c", "print('ok')"], require_running=False)
    assert state["up"] is True and result["ok"] is True and result["stdout"] == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="calls the Windows conversion obsidian.com uses")
@pytest.mark.parametrize("unit", ["x", '"', "⚙", "\U0001f600", "\ud800"])
def test_the_argument_limit_is_the_clients_conversion_buffer(unit: str) -> None:
    """obsidian.com converts each argument with WideCharToMultiByte(CP_UTF8, 0, arg, -1, buf,
    0x1000) and leaves out one that fails; `args_fit` must agree at the boundary."""
    import ctypes

    convert = ctypes.WinDLL("kernel32").WideCharToMultiByte  # type: ignore[attr-defined]
    convert.argtypes = [ctypes.c_uint, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_int]
    convert.argtypes += [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    buf = ctypes.create_string_buffer(0x1000)
    per = obsidian_run.arg_bytes(unit) - 1
    last_fit = (obsidian_run.ARG_LIMIT - 1) // per
    for count in (last_fit, last_fit + 1):
        arg = unit * count
        converted = convert(0xFDE9, 0, arg, -1, buf, 0x1000, None, None) > 0
        assert obsidian_run.args_fit(["eval", arg]) is converted, (count, per)


def test_windows_native_launch_is_guarded_in_tests(
    monkeypatch: pytest.MonkeyPatch, obsidian_guard: Any
) -> None:
    monkeypatch.setattr(obsidian_run, "running", lambda: False)
    monkeypatch.setattr(obsidian_run.sys, "platform", "win32")
    assert obsidian_run.launch(wait_s=0) is False  # refused by the guard, so reported not started
    assert obsidian_guard.blocked == [["startfile", "obsidian://open"]]
    obsidian_guard.blocked.clear()  # the refusal was this test's point
