"""Native Windows cleanup for the isolated Claude subprocess transport."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from agent_cli import core
from agent_cli.core import AgentError
from agent_cli.dispatch import isolated_transport

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows transport tree tests")


def test_audited_sdk_adapter_allows_windows():
    isolated_transport.check_compatibility()


def test_dead_parent_without_job_cannot_certify_descendant_cleanup(monkeypatch: pytest.MonkeyPatch):
    from typing import cast

    from anyio.abc import Process

    from agent_cli import win_tree

    class Finished:
        pid = 999_999_999
        returncode = 0

        async def wait(self):
            return self.returncode

    monkeypatch.setattr(win_tree, "kill_tree", lambda _pid: False)
    with pytest.raises(AgentError, match="cleanup could not be confirmed"):
        anyio.run(isolated_transport._reap, cast(Process, Finished()))


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not core.is_alive(pid):
            return True
        time.sleep(0.05)
    return not core.is_alive(pid)


@pytest.mark.parametrize("parent_waits", [True, False])
def test_reap_ends_grandchild_after_parent_exit_and_spares_unrelated(tmp_path: Path, parent_waits: bool):
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(61)']); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); "
        + ("time.sleep(60)" if parent_waits else "")
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    grandchild = None

    async def exercise() -> None:
        nonlocal grandchild
        process = await anyio.open_process(
            [sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
        )
        job = isolated_transport._windows_job(process)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                await anyio.sleep(0.05)
            grandchild = int(pidfile.read_text())
            await isolated_transport._reap(process, job)
            assert process.returncode is not None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await process.aclose()

    try:
        anyio.run(exercise)
        assert grandchild is not None and _gone(grandchild)
        assert unrelated.poll() is None
    finally:
        if grandchild is not None and core.is_alive(grandchild):
            from agent_cli.win_tree import kill_tree

            kill_tree(grandchild)
        unrelated.kill()
        unrelated.wait(timeout=5)
