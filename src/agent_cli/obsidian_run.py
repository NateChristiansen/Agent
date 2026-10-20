"""Running the Obsidian CLI without starting Obsidian on a pipe.

The Obsidian CLI is the app binary. With no Obsidian running, `obsidian eval ...` starts the full
app, and the app keeps the caller's stdin, stdout and stderr. Background tab painting ran it with
`capture_output=True` while the app was closed: each call launched Obsidian, the 10-second timeout
killed it, and the next retry launched it again (obsidian.log shows app starts 2 to 10 s apart).
An instance whose pipe reader had gone raised `write EPIPE` from every console write, which
Obsidian shows as "A JavaScript error occurred in the main process", over and over.

- `running()`: the app's CLI socket accepts a connection (the process list where it has none).
- `run()`: the CLI against a running app only (None when it is not running), with stdin from
  /dev/null and output to temporary files, never pipes, in its own session.
- `launch()`: an explicit open starts the app the way the OS does, detached, and waits for it.

One argument past 4 KB is a second dialog. The CLI sends its arguments to the app as a one-line
JSON header, which the app reads whole however the socket splits it. The Windows `obsidian.com`
builds that header itself: it writes the comma before each later argument, then converts the
argument to UTF-8 in a 4096-byte buffer, and skips one that does not fit. The app then parses
`["eval",]` and raises "SyntaxError: Unexpected token ']'" at `JSON.parse` in main.js:64:136, a
main-process dialog. Of the harness's calls only the drainer's batch eval grows that large, at
about a dozen tabs. `run()` refuses an argument over `ARG_LIMIT` (`ArgumentTooLong`) and the
drainer splits its batch.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

#: The socket a running Obsidian listens on for its CLI; the test suite points it elsewhere.
SOCKET_ENV = "AGENT_OBSIDIAN_CLI_SOCKET"
LAUNCH_WAIT_S = 15.0
ARG_LIMIT = 4096
"""The Windows client's conversion buffer, in the bytes `arg_bytes` counts."""


class ArgumentTooLong(OSError):
    """A CLI argument the Windows client would leave out of its header; nothing was sent."""


def arg_bytes(arg: str) -> int:
    """`arg` in UTF-8 with its terminating NUL, as the client converts it before any JSON
    escaping; a lone surrogate counts as the three bytes of its replacement character."""
    return len(arg.encode("utf-8", "surrogatepass")) + 1


def args_fit(args: list[str]) -> bool:
    return all(arg_bytes(arg) <= ARG_LIMIT for arg in args)


def running() -> bool:
    """Whether an Obsidian app is running to answer a CLI call."""
    answer = socket_answers(Path(os.environ.get(SOCKET_ENV) or Path.home() / ".obsidian-cli.sock"))
    if answer is not None:
        return answer
    from .obsidian import app_running

    return app_running()


def socket_answers(sock: Path) -> bool | None:
    """Whether the CLI socket accepts a connection; None where there is no such socket."""
    if os.name != "posix" or not sock.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(sock))
        return True
    except OSError:
        return False  # a stale socket: the app is gone


def run(
    argv: list[str], timeout: float, *, creationflags: int = 0
) -> subprocess.CompletedProcess[str] | None:
    """`argv` (an Obsidian CLI call) against a running app, or None when no app is running, so
    nothing here ever starts one. Raises `subprocess.TimeoutExpired` and `OSError` as
    `subprocess.run` does. One race remains: an app that quits between the check and the call
    lets that single call start it (and its timeout end it). The drainer checks again before every
    call, so that is one flash, not a loop, and no stdio of that instance is a pipe it can EPIPE on.
    Raises `ArgumentTooLong` without running anything for an argument over `ARG_LIMIT`."""
    if not args_fit(argv[1:]):
        longest = max(arg_bytes(arg) for arg in argv[1:])
        raise ArgumentTooLong(f"Obsidian CLI argument of {longest} bytes exceeds {ARG_LIMIT}")
    if not running():
        return None
    # stdin is /dev/null, as the detached drainer's has always been: 5,628 batches it painted read
    # their reply with it. A held-open stdin hangs instead, because the CLI forwards stdin to the
    # app's socket and the app keeps the connection open until that input ends.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(  # noqa: S603 — the caller's fixed argv
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
            # what it printed before the kill, such as Obsidian's outdated-installer banner
            exc.output, exc.stderr = _read(out), _read(err)
            raise
        stdout = _read(out).decode("utf-8", errors="replace")
        stderr = _read(err).decode("utf-8", errors="replace")
    # A test's stand-in for `subprocess.run` returns its own text; the real call wrote the files.
    if isinstance(result.stdout, str):
        stdout, stderr = result.stdout, str(result.stderr or "")
    return subprocess.CompletedProcess(argv, result.returncode, stdout, stderr)


def _read(spool: IO[bytes]) -> bytes:
    spool.seek(0)
    return spool.read()


def launch(wait_s: float = LAUNCH_WAIT_S) -> bool:
    """Start Obsidian for an explicit open, detached from this process, and wait until it answers.
    Whether it is running afterwards."""
    if running():
        return True
    try:
        if sys.platform == "win32":
            os.startfile("obsidian://open")  # type: ignore[attr-defined]  # noqa: S606
        else:
            argv = (
                ["open", "-g", "-a", "Obsidian"]
                if sys.platform == "darwin"
                else ["xdg-open", "obsidian://open"]
            )
            subprocess.Popen(  # noqa: S603
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
