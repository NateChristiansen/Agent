"""G08 synthetic Git worktrees and real local process boundaries; no model/network calls."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_cli import links
from agent_cli.core import AgentError
from agent_cli.dispatch import verification as runner
from agent_cli.dispatch import verification_process as procs
from agent_cli.dispatch.verification_evidence import Binding
from agent_cli.dispatch.verification_store import Store
from harness_root import rewrite
from verification_fixtures import (
    DESCENDANT,
    STORE_REJECTS,
    assessment,
    await_running,
    config,
    git,
    python_command,
    start,
)


def build(base: Path) -> tuple[Path, Path, str]:
    """The layout `verification_fixtures.fixture` builds: an empty harness root and a worktree
    of a one-commit repository."""
    root, repo, worktree = (base / name for name in ("harness", "repository", "worktree"))
    root.mkdir()
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "tracked").write_text("original\n")
    git(repo, "add", "tracked")
    git(repo, "commit", "-m", "fixture")
    git(repo, "worktree", "add", "-b", "verification-fixture", str(worktree))
    return root, worktree, git(worktree, "rev-parse", "HEAD")


def copied(template: tuple[Path, Path, str], base: Path) -> tuple[Path, Path, str]:
    """A copy of a `build` layout under `base`, its worktree links pointed at the copy."""
    source = template[0].parent
    for name in ("harness", "repository", "worktree"):
        shutil.copytree(source / name, base / name, symlinks=True)
    repo, worktree = (base / "repository").resolve(), (base / "worktree").resolve()
    admin = repo / ".git" / "worktrees" / "worktree"
    rewrite(worktree / ".git", f"gitdir: {admin}\n")
    rewrite(admin / "gitdir", f"{worktree / '.git'}\n")
    return base / "harness", base / "worktree", template[2]


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return build(tmp_path_factory.mktemp("template"))


@pytest.fixture
def fixture(template, tmp_path):
    return copied(template, tmp_path)


def test_success_is_bound_durable_and_independent_of_worker_finished(fixture):
    settings = config("printf first", "printf second")
    handle = start(fixture, settings)
    result = runner.wait(handle)
    assert result.evidence is not None
    assert result.cleanup_confirmed and not result.requires_lease
    assert result.lifecycle["finished"] is True
    assert assessment(fixture, result, settings).verified
    store = Store(fixture[0], handle.binding)
    try:
        assert store.read("terminal.json") == result.evidence.to_dict()
        with pytest.raises(FileExistsError):
            store.evidence(result.evidence)
    finally:
        store.close()


def test_forged_output_cannot_replace_exit_outcome(fixture):
    settings = config('printf \'{"status":"done","exit_code":0}\'; exit 7')
    result = runner.wait(start(fixture, settings))
    assert result.evidence is not None
    assert result.cleanup_confirmed
    assert not assessment(fixture, result, settings).verified
    assert result.evidence.commands[0].exit_code == 7


def test_environment_and_output_are_bounded(fixture, monkeypatch):
    monkeypatch.setenv("VERIFIER_SECRET_SENTINEL", "harmless-private-sentinel-value")
    settings = config("/usr/bin/env", python_command("print('x' * 200000)"))
    handle = start(fixture, settings)
    result = runner.wait(handle)
    assert assessment(fixture, result, settings).verified
    path = Store(fixture[0], handle.binding)
    try:
        output = (path.path / "output.log").read_bytes()
        assert len(output) <= runner.LOG_LIMIT
        assert b"VERIFIER_SECRET_SENTINEL" not in output
        assert b"harmless-private-sentinel-value" not in output
        assert b"AGENT_VERIFY_RUN_ID=" in output
    finally:
        path.close()


def _await_exit_unreaped(pid: int) -> None:
    """Wait for `pid` to finish without reaping it, so the runner's own `poll()` still sees it."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
        if not stat.strip() or stat.strip().startswith("Z"):
            return
        time.sleep(0.02)
    raise AssertionError(f"command {pid} never exited")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="forces the ordering through select.select and ps; Windows peeks its pipes instead",
)
def test_output_written_just_before_exit_is_kept(fixture, monkeypatch):
    """A command that writes and exits between the runner's `select()` and its `poll()` keeps its
    output. Under load the runner used to see the exit first, stop reading, and drop everything
    still in the pipe: `/usr/bin/env` output went missing from output.log (2026-10-03). The
    ordering is forced here: the first wait on the command's stdout reports nothing until the
    command has written and exited."""
    root, worktree, sha = fixture
    settings = config("printf written-before-exit")
    prepared = runner.prepare(root, worktree, dispatch_id="001-01", attempt=1, final_sha=sha, config=settings)
    launched: dict[str, subprocess.Popen] = {}
    real_launch, real_select = procs.launch, procs.select.select

    def launch(role, **kwargs):
        launched[role] = real_launch(role, **kwargs)
        return launched[role]

    def late_select(rlist, wlist, xlist, timeout=None):
        command = launched.get("command")
        if command is not None and rlist == [command.stdout] and not launched.get("held"):
            launched["held"] = command
            _await_exit_unreaped(command.pid)
            return [], [], []
        return real_select(rlist, wlist, xlist, timeout)

    monkeypatch.setattr(procs, "launch", launch)
    monkeypatch.setattr(procs, "parent_gone", lambda: False)  # pytest owns this process's stdin
    monkeypatch.setattr(procs.select, "select", late_select)
    store = Store(prepared.root, prepared.binding)
    life = {k: v for k, v in store.load_lifecycle().items() if k not in {"binding", "schema_version"}}
    request = {"environment": prepared.environment, "worktree": str(prepared.worktree)}
    log_path = root / "output.log"
    try:
        with log_path.open("wb") as log:
            result = runner._command(
                store, life, request, 0, "printf written-before-exit", 3.0, time.monotonic() + 15, log
            )
    finally:
        store.close()
    assert launched.get("held"), "the forced ordering never engaged"
    assert (result.outcome, result.exit_code) == ("exited", 0)
    assert log_path.read_bytes() == b"written-before-exit"


def test_a_descendant_holding_the_pipe_does_not_stall_the_drain(fixture):
    """The drain after exit reads what is buffered, never waits for EOF: a background descendant
    keeps the pipe open past the command's exit, and waiting on it would run the command into
    its timeout and record a passing command as failed."""
    settings = config("sleep 30 & printf done", command_timeout=10.0)
    handle = start(fixture, settings)
    result = runner.wait(handle)
    assert result.cleanup_confirmed
    assert assessment(fixture, result, settings).verified
    store = Store(fixture[0], handle.binding)
    try:
        assert (store.path / "output.log").read_bytes() == b"done"
    finally:
        store.close()


@pytest.mark.parametrize(
    "command",
    [
        "printf dirty >> tracked",
        "printf dirty >> tracked; git add tracked",
        "git -c user.name=Fixture -c user.email=fixture@example.invalid commit --allow-empty -m changed",
    ],
)
def test_tree_or_head_changes_reject_success(fixture, command):
    settings = config(command)
    result = runner.wait(start(fixture, settings))
    assert result.evidence is not None
    assert result.cleanup_confirmed
    assert not assessment(fixture, result, settings).verified


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_flags_are_rejected(fixture, flag):
    git(fixture[1], "update-index", flag, "tracked")
    (fixture[1] / "tracked").write_text("hidden dirty\n")
    tree = runner.observe_tree(fixture[1])
    assert not tree.tracked_clean and not tree.index_clean
    settings = config("touch should-not-run")
    result = runner.wait(start(fixture, settings))
    assert not (fixture[1] / "should-not-run").exists()
    assert not assessment(fixture, result, settings).verified


#: Long enough for the command to start its descendant on a busy machine (0.8 s was not), and well
#: under the descendant's 60 s sleep. Windows' descendant waits on /proc in a loop, so it gets more.
SLOW_START = 10.0 if sys.platform == "win32" else 2.0


def test_timeout_kills_background_descendant(fixture):
    # What is asserted is the kill, not its speed.
    settings = config(DESCENDANT, command_timeout=SLOW_START)
    result = runner.wait(start(fixture, settings))
    assert result.evidence is not None
    assert result.cleanup_confirmed
    assert not assessment(fixture, result, settings).verified
    pid = int((fixture[1] / "descendant.pid").read_text())
    assert procs.process_dead(pid)
    assert result.evidence.commands[0].outcome == "timeout"


def test_cancel_stops_verifier_despite_finished_worker_record(fixture):
    settings = config(DESCENDANT)
    handle = start(fixture, settings)
    state = await_running(handle)
    # The runner never reads the model worker record, including this premature completion.
    (fixture[0] / ".agent/dispatches/001-01.json").write_text(
        json.dumps({"finished": "earlier", "stop_reason": "result"})
    )
    assert state.requires_lease
    runner.request_cancel(handle.root, handle.binding)
    result = runner.wait(handle)
    assert result.evidence is not None
    assert result.cleanup_confirmed and not result.requires_lease
    assert result.evidence.state == "cancelled"
    assert not assessment(fixture, result, settings).verified


def test_guardian_sigkill_triggers_independent_watchdog(fixture):
    settings = config(DESCENDANT, command_timeout=10)
    handle = start(fixture, settings)
    state = await_running(handle)
    handle.process.kill()
    result = runner.wait(handle)
    assert result.evidence is not None
    assert result.cleanup_confirmed and not result.requires_lease
    assert procs.group_dead(state.lifecycle["command_pid"])
    assert procs.process_dead(int((fixture[1] / "descendant.pid").read_text()))
    assert result.evidence.state == "interrupted"
    assert not assessment(fixture, result, settings).verified


def test_watchdog_death_never_certifies_cleanup(fixture):
    settings = config(DESCENDANT, command_timeout=SLOW_START)
    handle = start(fixture, settings)
    state = await_running(handle)
    # Windows has no SIGKILL; os.kill there is TerminateProcess, which a process cannot catch.
    os.kill(state.lifecycle["watchdog_pid"], getattr(signal, "SIGKILL", signal.SIGTERM))
    # `runner.wait` would spend its whole cleanup grace waiting for a confirmation that cannot come.
    handle.process.wait(timeout=SLOW_START + 15)
    result = runner.recover(handle.root, handle.binding)
    assert not result.cleanup_confirmed and result.requires_lease
    assert not assessment(fixture, result, settings).verified
    assert procs.group_dead(state.lifecycle["command_pid"])
    assert procs.process_dead(int((fixture[1] / "descendant.pid").read_text()))


def test_prepare_publishes_before_exec_and_cancel_prevents_launch(fixture):
    root, worktree, sha = fixture
    prepared = runner.prepare(
        root, worktree, dispatch_id="001-01", attempt=1, final_sha=sha, config=config("touch forbidden")
    )
    status = runner.load_status(root, prepared.binding)
    assert status.requires_lease and not status.cleanup_confirmed
    assert not (worktree / "forbidden").exists()
    runner.request_cancel(root, prepared.binding)
    status = runner.recover(root, prepared.binding)
    assert status.cleanup_confirmed and not status.requires_lease
    with pytest.raises(Exception, match="cancelled, launched"):
        runner.launch_prepared(prepared)
    assert not (worktree / "forbidden").exists()


def external_parent(fixture, *, launch):
    root, worktree, sha = fixture
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = f"""import sys, json, signal
sys.path.insert(0, {source!r})
from pathlib import Path
from dataclasses import asdict
from agent_cli.dispatch import verification as v
from agent_cli.repos import VerificationCommand, VerificationConfig
p = v.prepare(Path({str(root)!r}), Path({str(worktree)!r}), dispatch_id='001-01', attempt=1,
 final_sha={sha!r}, config=VerificationConfig((VerificationCommand('sleep 60 & echo $! > descendant.pid; wait', 10),), 15))
if {launch!r}: h = v.launch_prepared(p)
print(json.dumps(asdict(p.binding)), flush=True)
signal.pause()
"""
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        env=procs.BASE_ENV,
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    assert parent.stdout is not None
    import select

    assert select.select([parent.stdout], [], [], 8)[0]
    binding = Binding(**json.loads(parent.stdout.readline()))
    return parent, binding


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the stand-in supervisor waits in signal.pause and is read with select on a pipe",
)
@pytest.mark.parametrize("launch", [False, True])
def test_supervisor_sigkill_before_and_after_launch_is_recoverable(fixture, launch):
    parent, binding = external_parent(fixture, launch=launch)
    try:
        if launch:
            deadline = time.monotonic() + 8
            while (
                runner.load_status(fixture[0], binding).lifecycle["phase"] != "running"
                or not (fixture[1] / "descendant.pid").exists()
            ):
                assert time.monotonic() < deadline
                time.sleep(0.02)
        else:
            assert runner.load_status(fixture[0], binding).requires_lease
        parent.kill()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 8
        while True:
            result = runner.recover(fixture[0], binding)
            if result.cleanup_confirmed:
                break
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert result.evidence is not None and result.evidence.state == "interrupted"
        assert not result.requires_lease
        if launch:
            assert procs.process_dead(int((fixture[1] / "descendant.pid").read_text()))
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if parent.stdout:
            parent.stdout.close()


def test_total_deadline_stops_later_commands(fixture):
    settings = config("sleep 0.3", "sleep 0.3", "touch forbidden", command_timeout=0.5, total=0.5)
    began = time.monotonic()
    result = runner.wait(start(fixture, settings))
    assert time.monotonic() - began < 8
    assert result.cleanup_confirmed
    assert not assessment(fixture, result, settings).verified
    assert not (fixture[1] / "forbidden").exists()


def test_git_observation_ignores_ambient_redirection_and_repo_helpers(fixture, monkeypatch):
    root, worktree, sha = fixture
    marker = root / "helper-ran"
    helper = root / "helper"
    helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    helper.chmod(0o755)
    for key in (
        "core.fsmonitor",
        "diff.fixture.textconv",
        "diff.external",
        "filter.fixture.clean",
        "filter.fixture.process",
    ):
        git(worktree, "config", key, str(helper))
    (worktree / ".gitattributes").write_text("tracked diff=fixture filter=fixture\n")
    (worktree / "tracked").write_text("dirty\n")
    monkeypatch.setenv("GIT_DIR", str(root / "wrong"))
    monkeypatch.setenv("GIT_WORK_TREE", str(root))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "broken")
    tree = runner.observe_tree(worktree)
    assert tree.head == sha and not tree.tracked_clean
    assert not marker.exists()


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="a file symlink needs Developer Mode or admin on Windows"
            ),
        ),
        pytest.param(
            "fifo", marks=pytest.mark.skipif(sys.platform == "win32", reason="Windows has no mkfifo")
        ),
        "directory",
    ],
)
def test_store_rejects_special_snapshots_without_blocking(fixture, entry):
    prepared = runner.prepare(
        fixture[0], fixture[1], dispatch_id="001-01", attempt=1, final_sha=fixture[2], config=config("true")
    )
    store = Store(fixture[0], prepared.binding)
    try:
        target = store.path / "special.json"
        if entry == "symlink":
            target.symlink_to(fixture[0] / "outside")
        elif entry == "fifo":
            os.mkfifo(target)
        else:
            target.mkdir()
        began = time.monotonic()
        with pytest.raises(STORE_REJECTS):
            store.read("special.json")
        with pytest.raises(STORE_REJECTS):
            store.write("special.json", {})
        assert time.monotonic() - began < 1
    finally:
        store.close()


@pytest.mark.agent_layout_unchecked(
    "`.agent` is a symlink to the worktree on purpose: the runner must refuse it"
)
def test_symlinked_storage_directory_rejects_before_execution(fixture):
    links.make(fixture[0] / ".agent", fixture[1], is_dir=True)  # a junction on Windows
    with pytest.raises((OSError, AgentError) if sys.platform == "win32" else OSError):
        start(fixture, config("touch forbidden"))
    assert not (fixture[1] / "forbidden").exists()


def test_malformed_and_replayed_metadata_fail_closed(fixture):
    first = runner.wait(start(fixture, config("true")))
    second = runner.prepare(
        fixture[0], fixture[1], dispatch_id="001-01", attempt=2, final_sha=fixture[2], config=config("true")
    )
    assert first.evidence is not None
    store = Store(fixture[0], second.binding)
    try:
        store.write("terminal.json", first.evidence.to_dict(), immutable=True)
        with pytest.raises(ValueError, match="stale verifier evidence"):
            runner.load_status(fixture[0], second.binding)
        store.write("lifecycle.json", {"schema_version": 99})
        with pytest.raises(ValueError, match="invalid or stale verifier lifecycle"):
            runner.load_status(fixture[0], second.binding)
    finally:
        store.close()


def test_recovery_cannot_abort_published_guardian_as_unlaunched_preparation(fixture):
    prepared = runner.prepare(
        fixture[0], fixture[1], dispatch_id="001-01", attempt=1, final_sha=fixture[2], config=config("true")
    )
    store = Store(fixture[0], prepared.binding)
    try:
        life = {k: v for k, v in store.load_lifecycle().items() if k not in {"schema_version", "binding"}}
        life["guardian_pid"] = os.getpid()  # Simulate the live PID publication barrier before guardian setup.
        store.lifecycle(life)
        runner.request_cancel(fixture[0], prepared.binding)
        result = runner.recover(fixture[0], prepared.binding)
        assert result.active and result.requires_lease
        assert store.read("terminal.json") is None
        life["guardian_pid"] = None
        store.lifecycle(life)
        assert runner.recover(fixture[0], prepared.binding).cleanup_confirmed
    finally:
        store.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no mkfifo")
def test_guardian_death_during_fifo_git_observation_self_expires(fixture):
    common = fixture[0].parent / "repository" / ".git"
    original, saved = common / "config", common / "saved-config"
    original.rename(saved)
    os.mkfifo(original)
    handle = None
    try:
        handle = start(fixture, config("touch forbidden"))
        deadline = time.monotonic() + 5
        git_pid = None
        while git_pid is None:
            code, output = procs.capture(
                ["/bin/ps", "-axo", "pid=,pgid=,comm="], Path("/"), procs.BASE_ENV, 1
            )
            assert code == 0
            for line in output.decode().splitlines():
                fields = line.split(maxsplit=2)
                if len(fields) == 3 and fields[1] == str(handle.process.pid) and fields[2].endswith("git"):
                    git_pid = int(fields[0])
            assert time.monotonic() < deadline
            time.sleep(0.01)
        handle.process.kill()
        handle.process.wait(timeout=2)
        pending = runner.recover(handle.root, handle.binding)
        assert pending.requires_lease and pending.lifecycle["cleanup_confirmed"] is False
        result = runner.wait(handle)
        assert result.lifecycle["cleanup_confirmed"] is True
        assert result.evidence is not None and result.evidence.state == "interrupted"
        assert result.cleanup_confirmed and not result.requires_lease
        assert procs.process_dead(git_pid)
        assert not (fixture[1] / "forbidden").exists()
    finally:
        if handle is not None and handle.process.poll() is None:
            handle.process.kill()
            handle.process.wait(timeout=2)
        original.unlink()
        saved.rename(original)


class _Recorded(Exception):
    """Raised in place of starting a role once `launch` has handed over its argv."""


@pytest.mark.parametrize("role", ["guardian", "command", "watchdog"])
def test_a_started_role_does_not_import_the_dispatch_package_facade(tmp_path, role):
    """Each role cold-starts its own Python. The facade's eager imports more than doubled that
    start, and under load a command's deadline passed before its watchdog was ready (2026-10-17).
    The role runs here from the argv `launch` builds, to the end of its request read."""
    argv: list[str] = []

    def record(args, **_kwargs):
        argv.extend(args)
        raise _Recorded

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "Popen", record)
        with pytest.raises(_Recorded):
            procs.launch(role, env=procs.BASE_ENV, cwd=tmp_path)
    assert argv[1:3] == ["-I", "-c"]
    report = (
        "import atexit, json, sys; atexit.register(lambda: print(json.dumps({"
        "'facade': hasattr(sys.modules.get('agent_cli.dispatch'), '__all__'), "
        "'modules': sorted(m for m in sys.modules if m.startswith('agent_cli.'))})))\n"
    )
    started = subprocess.run(
        [*argv[:-1], report + argv[-1]],
        cwd=tmp_path,
        env=procs.BASE_ENV,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert started.returncode == 2, started.stderr  # `read_request` refusing the empty request
    loaded = json.loads(started.stdout)
    module = "verification" if role == "guardian" else "verification_process"
    assert f"agent_cli.dispatch.{module}" in loaded["modules"]
    assert not loaded["facade"]
    assert "agent_cli.dispatch.closing" not in loaded["modules"]


def test_prepared_repr_hides_explicit_environment_values(fixture):
    prepared = runner.prepare(
        fixture[0],
        fixture[1],
        dispatch_id="001-01",
        attempt=1,
        final_sha=fixture[2],
        config=config("true"),
        environment={"SENTINEL": "private-fixture"},
    )
    assert "private-fixture" not in repr(prepared)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no mkfifo for the nested-git trap")
def test_gitlink_is_explicitly_unsupported_before_diff_or_nested_git(fixture, monkeypatch):
    root, worktree, sha = fixture
    nested = worktree / "nested"
    nested.mkdir()
    git(nested, "init", "-b", "main")
    git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{sha},nested")
    git(worktree, "commit", "-m", "synthetic gitlink")
    updated = (root, worktree, git(worktree, "rev-parse", "HEAD"))
    nested_config = nested / ".git/config"
    nested_config.unlink()
    os.mkfifo(nested_config)
    calls = []
    original = procs.capture

    def observe_only(argv, *args, **kwargs):
        calls.append(argv)
        assert "diff" not in argv
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(procs, "capture", observe_only)
    with pytest.raises(procs.UnsupportedTree, match="gitlink/submodule"):
        runner.observe_tree(worktree)
    assert any("ls-files" in argv for argv in calls)
    result = runner.wait(start(updated, config("touch forbidden")))
    assert result.lifecycle["reason"] == "unsupported_tree"
    assert result.cleanup_confirmed
    assert not (worktree / "forbidden").exists()
