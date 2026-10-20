"""G04: real Claude SDK subprocess environment-boundary regression evidence.

The worker process is a local fixture stub. It receives no model prompt result,
authentication, credential value, or network access; it records only presence bits for
selected harmless sentinels and exits. G05 exercises the harness isolation adapter.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_cli import core, dispatch
from dispatch_helpers import PROMPT, droot  # noqa: F401 — imported pytest fixture
from fixtures.claude_environment_stub import SELECTED_NAMES, captures, write_stub

CLI_NAME = "claude.exe" if sys.platform == "win32" else "claude"


def _kill_group(pid: int, sig: int) -> None:
    if sys.platform == "win32":
        from agent_cli import win_tree

        win_tree.kill_tree(pid)
    else:
        os.killpg(pid, sig)


def _install_boundary_stub(tmp_path: Path, monkeypatch, *, mode: str = "result") -> Path:
    bin_dir = tmp_path / "boundary-bin"
    bin_dir.mkdir()
    _stub, capture = write_stub(bin_dir, mode=mode)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("G04_SECRET_SENTINEL", "harmless-secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/g04")
    monkeypatch.setenv("G04_NOTIFICATION_SENTINEL", "harmless-notification")
    monkeypatch.setenv("AGENT_SLACK_WEBHOOK", "https://example.invalid/fixture")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "manager-only")
    return capture


def _assert_environment_unchanged(before: dict[str, str]) -> None:
    # Assertion diagnostics expose names only, never ambient credential values.
    changed_names = sorted(
        name for name in set(before) | set(os.environ) if before.get(name) != os.environ.get(name)
    )
    assert changed_names == []


def _by_kind(capture: Path, kind: str) -> dict[str, Any]:
    rows = [row for row in captures(capture) if row["kind"] == kind]
    assert rows, f"stub did not record a {kind} process"
    return rows[-1]


def test_real_sdk_watch_child_excludes_sentinels(droot, fake_claude, monkeypatch, tmp_path):
    """`start(..., watch=True)` reaches the real SDK version and worker subprocesses."""
    capture = _install_boundary_stub(tmp_path, monkeypatch)
    monkeypatch.setattr(dispatch.supervisor, "transport_factory", None)
    before = {name: os.environ.get(name) for name in SELECTED_NAMES}

    result = dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=True, no_checks=True)

    # The fixture supplies a valid blocked result so the launch finishes without a model.
    # This test observes the actual SDK child boundary through the harness adapter.
    assert result["state"] in {"blocked", "failed"}
    version = _by_kind(capture, "version")["present"]
    worker = _by_kind(capture, "worker")["present"]
    assert isinstance(version, dict) and isinstance(worker, dict)
    assert version["G04_SECRET_SENTINEL"] is False
    assert version["ANTHROPIC_BASE_URL"] is False
    assert version["G04_NOTIFICATION_SENTINEL"] is False
    assert worker["G04_SECRET_SENTINEL"] is False
    assert worker["ANTHROPIC_BASE_URL"] is False
    assert worker["G04_NOTIFICATION_SENTINEL"] is False
    assert worker["AGENT_DISPATCH_ID"] is True
    assert worker["CLAUDE_CODE_ENTRYPOINT"] is True
    assert worker["CLAUDE_AGENT_SDK_VERSION"] is True
    assert {name: os.environ.get(name) for name in SELECTED_NAMES} == before


def _assert_descendant_stopped(capture: Path) -> None:
    pid = _by_kind(capture, "descendant")["pid"]
    for _ in range(100):
        if sys.platform == "win32":
            if not core.is_alive(pid):
                return
        else:
            result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
            if not result.stdout.strip() or result.stdout.strip().startswith("Z"):
                return
        time.sleep(0.02)
    raise AssertionError(f"fixture descendant {pid} survived transport cleanup")


def test_harness_connect_failure_cleans_descendants(droot, fake_claude, monkeypatch, tmp_path):
    capture = _install_boundary_stub(tmp_path, monkeypatch, mode="connect-failure")
    monkeypatch.setattr(dispatch.supervisor, "transport_factory", None)
    before = dict(os.environ)
    result = dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=True, no_checks=True)
    assert result["state"] == "failed"
    assert _by_kind(capture, "worker")["present"]["G04_SECRET_SENTINEL"] is False
    _assert_descendant_stopped(capture)
    _assert_environment_unchanged(before)


def test_harness_cancel_cleans_descendants(droot, fake_claude, monkeypatch, tmp_path):
    import threading

    capture = _install_boundary_stub(tmp_path, monkeypatch, mode="cancel")
    monkeypatch.setattr(dispatch.supervisor, "transport_factory", None)
    before = dict(os.environ)

    def cancel_after_child():
        for _ in range(1500):
            if any(row["kind"] == "descendant" for row in captures(capture)):
                record = dispatch.load(droot, "001-mac-01")
                record["state"] = "cancelled"
                dispatch.state.save(droot, record)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=cancel_after_child, daemon=True)
    thread.start()
    result = dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=True, no_checks=True)
    thread.join(timeout=5)
    assert result["state"] == "cancelled"
    assert _by_kind(capture, "interrupted")["present"]["G04_SECRET_SENTINEL"] is False
    descendant = _by_kind(capture, "descendant")["present"]
    assert descendant["G04_SECRET_SENTINEL"] is False
    assert descendant["AGENT_DISPATCH_ID"] is True
    _assert_descendant_stopped(capture)
    _assert_environment_unchanged(before)


def test_detach_keeps_non_claude_sentinels_and_drops_claude_identity(monkeypatch, tmp_path):
    """`start(..., watch=False)` constructs the supervisor environment without launching it."""
    seen: dict[str, str] = {}

    def popen(_argv, **kwargs):
        seen.update(kwargs["env"])
        return SimpleNamespace(pid=424242)

    monkeypatch.setenv("G04_SECRET_SENTINEL", "harmless-secret")
    monkeypatch.setenv("G04_NOTIFICATION_SENTINEL", "harmless-notification")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "manager-only")
    monkeypatch.setattr(dispatch.supervisor, "popen", popen)
    (tmp_path / ".agent" / "dispatches").mkdir(parents=True)
    d = {"id": "001-mac-01"}

    dispatch.state.save(tmp_path, d)
    dispatch.supervisor.start(tmp_path, d, watch=False)

    assert "G04_SECRET_SENTINEL" in seen
    assert "G04_NOTIFICATION_SENTINEL" in seen
    assert "CLAUDE_SESSION_ID" not in seen
    assert seen["AGENT_DISPATCH_ID"] == "001-mac-01"


def test_real_detached_supervisor_reaches_the_sdk_child(droot, fake_claude, monkeypatch, tmp_path):
    """The detached supervisor process reaches the same local, presence-only fixture."""
    capture = _install_boundary_stub(tmp_path, monkeypatch)
    before = {name: os.environ.get(name) for name in SELECTED_NAMES}
    source = Path(__file__).resolve().parents[1] / "src"
    children: list[subprocess.Popen] = []

    def popen(_argv, **kwargs):
        env = dict(kwargs["env"])
        env["PYTHONPATH"] = f"{source}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
        proc = subprocess.Popen(
            [sys.executable, "-c", "from agent_cli.cli import main; main()", "dispatch", "run", "001-mac-01"],
            **{**kwargs, "env": env},
        )
        children.append(proc)
        return proc

    monkeypatch.setattr(dispatch.supervisor, "popen", popen)
    try:
        started = dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=False, no_checks=True)
        assert started["state"] == "queued"
        for _ in range(1500):
            if any(row["kind"] == "worker" for row in captures(capture)):
                break
            time.sleep(0.02)
        # A cold supervisor still has evaluate, finalize, render and its own `ps` reap to do
        # after the worker reports. One 5 s timeout under load was never reproduced; the longer
        # wait mitigates it without proving its cause.
        assert children and children[0].wait(timeout=30) == 0
        worker = _by_kind(capture, "worker")["present"]
        assert worker["G04_SECRET_SENTINEL"] is False
        assert worker["ANTHROPIC_BASE_URL"] is False
        assert worker["G04_NOTIFICATION_SENTINEL"] is False
        assert worker["AGENT_DISPATCH_ID"] is True
        assert worker["CLAUDE_CODE_ENTRYPOINT"] is True
        assert worker["CLAUDE_AGENT_SDK_VERSION"] is True
        assert {name: os.environ.get(name) for name in SELECTED_NAMES} == before
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    _kill_group(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        _kill_group(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=2)


def test_harness_permission_callback_retains_stdio_routing(droot, fake_claude, monkeypatch, tmp_path):
    capture = _install_boundary_stub(tmp_path, monkeypatch, mode="callback")
    monkeypatch.setattr(dispatch.supervisor, "transport_factory", None)
    dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=True, no_checks=True)
    assert _by_kind(capture, "worker")["stdio_permission"] is True
    assert _by_kind(capture, "permission")["behavior"] == "deny"
    assert dispatch.load(droot, "001-mac-01")["permission_events"]


def test_concurrent_harness_launch_keeps_notification_and_peer_environment(tmp_path, monkeypatch):
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions

    from agent_cli.dispatch.driver import _drive_worker
    from agent_cli.dispatch.options import worker_env

    capture = _install_boundary_stub(tmp_path, monkeypatch, mode="hold")
    monkeypatch.setattr(dispatch.supervisor, "transport_factory", None)
    before = dict(os.environ)
    root = tmp_path / "harness"
    records = []
    for index in (1, 2):
        record = {"id": f"001-0{index}", "feature": 1, "session_id": f"fixture-{index}", "state": "running"}
        dispatch.state.save(root, record)
        records.append(record)

    async def run():
        tasks = [
            asyncio.create_task(
                _drive_worker(
                    root,
                    record,
                    ClaudeAgentOptions(
                        cli_path=str(capture.parent / CLI_NAME), cwd=str(root), env=worker_env(root, record)
                    ),
                    first_message="fixture",
                    inbox=root / f"inbox-{record['id']}",
                    timeout=30,
                    idle_seconds=30,
                )
            )
            for record in records
        ]
        try:
            for _ in range(2000):
                _assert_environment_unchanged(before)
                assert os.environ.get("G04_NOTIFICATION_SENTINEL") == "harmless-notification"
                if sum(row["kind"] == "worker" for row in captures(capture)) == 2:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("both concurrent CLI processes must reach the hold barrier")
            assert not any(task.done() for task in tasks)
            for _ in range(5):
                _assert_environment_unchanged(before)
                assert os.environ.get("G04_NOTIFICATION_SENTINEL") == "harmless-notification"
                await asyncio.sleep(0.01)
        finally:
            Path(str(capture) + ".release").touch()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert all(result["stop_reason"] == "result" for result in results)

    asyncio.run(run())
    for row in captures(capture):
        assert row["present"]["AGENT_SLACK_WEBHOOK"] is False
        assert row["present"]["CLAUDE_SESSION_ID"] is False
        assert row["present"]["G04_SECRET_SENTINEL"] is False
        assert row["present"]["CLAUDE_CODE_ENTRYPOINT"] is True
    _assert_environment_unchanged(before)
