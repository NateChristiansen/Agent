"""Native Windows proof that bounded commands own their descendants."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_cli import core, procs

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process job tests")


def _wait_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not core.is_alive(pid):
            return True
        time.sleep(0.05)
    return not core.is_alive(pid)


@pytest.mark.parametrize("parent_waits", [True, False])
def test_timeout_ends_owned_grandchild_and_spares_unrelated(tmp_path: Path, parent_waits: bool):
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(61)']); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); "
        + ("time.sleep(60)" if parent_waits else "")
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            procs.run_bounded([sys.executable, "-c", code], timeout=1)
        grandchild = int(pidfile.read_text())
        assert _wait_gone(grandchild)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait()


def test_rejected_job_assignment_keeps_short_command_usable(monkeypatch: pytest.MonkeyPatch):
    from agent_cli import win_tree

    def reject(_process: object) -> None:
        raise OSError("host job policy rejects assignment")

    monkeypatch.setattr(win_tree, "Job", reject)
    result = procs.run_bounded([sys.executable, "-c", "print('ok')"], timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "ok"


def test_failed_fallback_keeps_timeout_contract_and_reports_uncertain_cleanup(
    monkeypatch: pytest.MonkeyPatch,
):
    from agent_cli import win_tree

    def reject(_process: object) -> None:
        raise OSError("host job policy rejects assignment")

    monkeypatch.setattr(win_tree, "Job", reject)
    monkeypatch.setattr(win_tree, "kill_tree", lambda _pid: False)
    with pytest.raises(procs.CleanupUnconfirmed, match="cleanup could not be confirmed") as caught:
        procs.run_bounded([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1)
    assert isinstance(caught.value, subprocess.TimeoutExpired)


def test_worktree_reports_uncertain_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli import repos
    from agent_cli.dispatch import worktrees

    def fail(*_args: object, **_kwargs: object) -> None:
        raise procs.CleanupUnconfirmed("setup", 1)

    monkeypatch.setattr(procs, "run_bounded", fail)
    repo = repos.Repo(name="x", short="x", path="x", remote="r", dispatch={"setup": ["setup"]})
    (result,) = worktrees.run_worktree_setup(tmp_path, repo, tmp_path)
    assert "cleanup unconfirmed" in result["error"]


@pytest.mark.parametrize("error", [OSError("missing taskkill"), subprocess.TimeoutExpired("taskkill", 10)])
def test_taskkill_failure_is_reported_as_false(monkeypatch: pytest.MonkeyPatch, error: Exception):
    from agent_cli import win_tree

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(win_tree.subprocess, "run", fail)
    assert win_tree.kill_tree(999_999) is False
