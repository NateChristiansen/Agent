"""`peer-net pair` and `peer-net accept` at the CLI: the owner's prompt, accept's clean exit,
and the remote exit status reaching pair's result (tasks 14 and 15)."""

from __future__ import annotations

import argparse
import builtins
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_cli.cli import peer_net as cli
from agent_cli.peer_net import bootstrap, ssh
from peer_net_pair_fixture import Devices, make_devices

REPO = Path(__file__).resolve().parent.parent


def test_the_fingerprint_prompt_names_the_other_device_and_where_its_key_was_printed():
    prompt = cli.confirm_prompt("mac", "192.0.2.107", "SHA256:abc")
    assert "mac at 192.0.2.107" in prompt and "SHA256:abc" in prompt
    assert "host_fingerprints" in prompt and "printed on mac" in prompt and "not on this machine" in prompt


@pytest.mark.parametrize(
    ("remote_exit", "code"),
    [(0, 0), (None, 0), (1, 3), (-9, 3), (255, 3)],
    ids=["clean", "unknown", "accept failed", "killed", "ssh failed"],
)
def test_pair_fails_when_the_remote_side_exited_badly_even_if_both_routes_verified(remote_exit, code):
    text, exit_code = cli.pair_report(
        {"to": "mac", "verified": True, "their_probe": {"verified": True}, "remote_exit": remote_exit}
    )
    assert exit_code == code
    assert ("exited" in text) is (code == 3)


class _Reader:
    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def join(self, timeout: float) -> None:
        pass

    def is_alive(self) -> bool:
        return self.alive


class _Channel:
    def __init__(self, alive: bool, writing: bool = False) -> None:
        self.reader = _Reader(alive)
        self.writing = writing


def test_accept_exits_without_interpreter_shutdown_only_while_a_stream_thread_is_blocked():
    exits: list[int] = []
    assert cli.accept_exit(_Channel(alive=False), 0, hard_exit=exits.append) == 0
    assert exits == []
    cli.accept_exit(_Channel(alive=True), 1, hard_exit=exits.append)
    cli.accept_exit(_Channel(alive=False, writing=True), 0, hard_exit=exits.append)
    assert exits == [1, 0]


class _Stuck(io.RawIOBase):
    def __init__(self) -> None:
        self.release = threading.Event()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.release.wait(10)
        return len(data)


def test_a_blocked_writer_with_an_ended_reader_still_takes_the_hard_exit():
    from agent_cli.peer_net.channel import LineChannel
    from agent_cli.peer_net.errors import PeerNetError

    r, w = os.pipe()
    os.close(w)  # input already at EOF: the reader ends at once
    stuck = _Stuck()
    channel = LineChannel(os.fdopen(r, "rb"), stuck, write_timeout_s=0.1)  # type: ignore[arg-type]
    with pytest.raises(PeerNetError):
        channel.send({"v": 1, "phase": "bye", "pair_id": "a" * 32}, 5)  # the pipe is never read
    channel.close()
    channel.reader.join(2)
    assert not channel.reader.is_alive() and channel.writing
    exits: list[int] = []
    started = time.monotonic()
    cli.accept_exit(channel, 0, hard_exit=exits.append)
    assert exits == [0] and time.monotonic() - started < 2
    stuck.release.set()


@pytest.mark.skipif(os.name == "nt", reason="the Windows entry checks accept.cmd first")
def test_a_real_accept_with_stdin_held_open_exits_in_bounded_time_without_a_fatal_error(tmp_path):
    # Live on 2026-10-04 the Mac's accept aborted at shutdown: its reader thread still held
    # the stdin lock. Here stdin stays open after accept is done, as when pair is slow to
    # close its end. Output goes to files, so nothing here closes stdin early.
    out_path, err_path = tmp_path / "out", tmp_path / "err"
    with out_path.open("wb") as out_file, err_path.open("wb") as err_file:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(REPO / "bin" / "peer-net-entry"), "peer-net", "accept"],
            stdin=subprocess.PIPE,
            stdout=out_file,
            stderr=err_file,
            env={**os.environ, "SSH_CONNECTION": "192.0.2.5 1 192.0.2.9 22"},
        )
        assert proc.stdin is not None
        proc.stdin.write(b'{"v": 1, "phase": "hello", "pair_id": "short"}\n')  # refused before any write
        proc.stdin.flush()
        started = time.monotonic()
        try:
            proc.wait(timeout=cli.ACCEPT_EOF_WAIT_S + 30)  # stdin is still open throughout
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdin.close()
    err = err_path.read_bytes()
    assert proc.returncode == 1, err
    assert b"Fatal Python error" not in err and b"accept refused: pair-protocol" in err
    assert time.monotonic() - started < cli.ACCEPT_EOF_WAIT_S + 30
    assert b'"phase":"error"' in out_path.read_bytes()


@pytest.fixture
def devices(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Devices:
    return make_devices(tmp_root, tmp_path, monkeypatch)


def _args(**over) -> argparse.Namespace:
    return argparse.Namespace(
        **{
            "alias": "mac",
            "target": "user@192.0.2.9",
            "repo": "/home/user/workspace/Agent",
            "port": 22,
            "device": "win",
            "enable": True,
            "restart": False,
            "login_key": None,
            "self_user": "user",
            "self_port": 22,
            **over,
        }
    )


@pytest.mark.parametrize("remote_exit", [0, 1, -9])
def test_the_remote_exit_status_reaches_pairs_result(devices, monkeypatch, remote_exit):
    def fake_session(command, deadline_s):
        assert command[0] == ssh.binary("ssh") and command[-1].endswith("peer-net accept")
        end = devices.session()
        end.returncode = remote_exit  # what the closed and reaped ssh process reports
        return end

    monkeypatch.setattr(bootstrap, "SshSession", fake_session)
    monkeypatch.setattr(builtins, "input", lambda prompt: "y")
    result = cli.pair(devices.a, _args())
    if devices.b_thread is not None:
        devices.b_thread.join(10)
    assert result["verified"] is True and result["their_probe"]["verified"] is True
    assert result["remote_exit"] == remote_exit
    assert cli.pair_report(result)[1] == (0 if remote_exit == 0 else 3)


def test_a_stuck_reader_thread_is_the_case_accept_exit_handles():
    # The live failure's precondition: a daemon reader blocked on a stream that never ends.
    r, w = os.pipe()
    from agent_cli.peer_net.channel import LineChannel

    channel = LineChannel(os.fdopen(r, "rb"), io.BytesIO())
    exits: list[int] = []
    started = time.monotonic()
    original = cli.ACCEPT_EOF_WAIT_S
    cli.ACCEPT_EOF_WAIT_S = 0.2
    try:
        cli.accept_exit(channel, 0, hard_exit=exits.append)
    finally:
        cli.ACCEPT_EOF_WAIT_S = original
    assert exits == [0] and time.monotonic() - started < 2
    os.close(w)  # now EOF: the reader ends
    channel.reader.join(2)
    assert not channel.reader.is_alive()
