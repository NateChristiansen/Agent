"""Shared worktree fixture and helpers for the verification tests.

Split out of test_verification_runner.py at the 500-line gate, so the runner tests and the
status-reading tests can use one fixture instead of two copies of it.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_cli.core import AgentError
from agent_cli.dispatch import verification as runner
from agent_cli.dispatch import verification_process as procs
from agent_cli.dispatch.verification_evidence import assess
from agent_cli.repos import VerificationCommand, VerificationConfig
from harness_root import repoint_files

# A background `sleep` whose pid lands in descendant.pid. Git Bash's `$!` is an MSYS pid; the
# runner's process checks take the Windows pid, which MSYS keeps in /proc. It is read once the
# child has exec'd sleep: before that it names the forked bash, which exits at the exec.
DESCENDANT = (
    "sleep 60 & until read exe < /proc/$!/exename; [[ $exe == */sleep ]]; do :; done; "
    "read pid < /proc/$!/winpid; echo $pid > descendant.pid; wait"
    if sys.platform == "win32"
    else "sleep 60 & echo $! > descendant.pid; wait"
)
#: The error a Windows store raises for a reparse point or non-regular file (`win_lock`).
STORE_REJECTS = (OSError, ValueError, AgentError) if sys.platform == "win32" else (OSError, ValueError)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        env={**procs.BASE_ENV, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


#: The two files a linked worktree keeps its absolute paths in.
_WORKTREE_LINKS = ("repository/.git/worktrees/worktree/gitdir", "worktree/.git")


_TEMPLATE: dict[Path, tuple[Path, str]] = {}


def _fixture_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A repository with one commit and a linked worktree on its own branch, built once per
    worker: seven git calls per test were most of the fixture's cost. Cached here rather than
    in a session fixture so the modules that import `fixture` need not import a second name."""
    key = tmp_path_factory.getbasetemp()
    if key not in _TEMPLATE:
        _TEMPLATE[key] = _build_template(tmp_path_factory.mktemp("verification-template"))
    return _TEMPLATE[key]


def _build_template(base: Path) -> tuple[Path, str]:
    repo, worktree = base / "repository", base / "worktree"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "tracked").write_text("original\n")
    git(repo, "add", "tracked")
    git(repo, "commit", "-m", "fixture")
    git(repo, "worktree", "add", "-b", "verification-fixture", str(worktree))
    return base, git(worktree, "rev-parse", "HEAD")


@pytest.fixture
def fixture(tmp_path, tmp_path_factory):
    template, sha = _fixture_template(tmp_path_factory)
    root, repo, worktree = (tmp_path / name for name in ("harness", "repository", "worktree"))
    root.mkdir()
    shutil.copytree(template / "repository", repo, symlinks=True)
    shutil.copytree(template / "worktree", worktree, symlinks=True)
    repoint_files(tmp_path, template, _WORKTREE_LINKS)
    return root, worktree, sha


def config(*commands: str, command_timeout=3.0, total=15.0):
    return VerificationConfig(
        tuple(VerificationCommand(command, command_timeout) for command in commands), total
    )


def start(fixture, settings, **kwargs):
    root, worktree, sha = fixture
    return runner.start(
        root, worktree, dispatch_id="001-01", attempt=1, final_sha=sha, config=settings, **kwargs
    )


def assessment(fixture, result, settings):
    assert result.evidence is not None
    return assess(
        result.evidence,
        expected=result.evidence.binding,
        config=settings,
        current_tree=runner.observe_tree(fixture[1]),
    )


def await_running(handle):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = runner.load_status(handle.root, handle.binding)
        marker = handle.root.parent / "worktree" / "descendant.pid"
        if state.lifecycle["phase"] == "running" and marker.exists():
            assert not procs.process_dead(int(marker.read_text()))
            return state
        if handle.process.poll() is not None:
            raise AssertionError(state.lifecycle)
        time.sleep(0.02)
    raise AssertionError("verifier never reached running command")


def python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -I -c {shlex.quote(code)}"
