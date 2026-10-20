# ruff: noqa
from __future__ import annotations
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

SOCKET_ENV = "AGENT_OBSIDIAN_CLI_SOCKET"
LAUNCH_WAIT_S = 15.0


def running() -> bool:
    answer = socket_answers(Path(os.environ.get(SOCKET_ENV) or Path.home() / ".obsidian-cli.sock"))
    if answer is not None:
        return answer
    from .obsidian import app_running

    return app_running()


def socket_answers(sock: Path) -> bool | None:
    if os.name != "posix" or not sock.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(sock))
        return True
    except OSError:
        return False


def run(
    argv: list[str], timeout: float, *, creationflags: int = 0
) -> subprocess.CompletedProcess[str] | None:
    if not running():
        return None
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=os.name == "posix",
                creationflags=creationflags,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            exc.output, exc.stderr = (_read(out), _read(err))
            raise
        stdout = _read(out).decode("utf-8", errors="replace")
        stderr = _read(err).decode("utf-8", errors="replace")
    if isinstance(result.stdout, str):
        stdout, stderr = (result.stdout, str(result.stderr or ""))
    return subprocess.CompletedProcess(argv, result.returncode, stdout, stderr)


def _read(spool: IO[bytes]) -> bytes:
    spool.seek(0)
    return spool.read()


def launch(wait_s: float = LAUNCH_WAIT_S) -> bool:
    if running():
        return True
    try:
        if sys.platform == "win32":
            os.startfile("obsidian://open")
        else:
            argv = (
                ["open", "-g", "-a", "Obsidian"]
                if sys.platform == "darwin"
                else ["xdg-open", "obsidian://open"]
            )
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError:
        return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if running():
            return True
        time.sleep(0.25)
    return False
