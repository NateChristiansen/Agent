"""Behavioral integration of supervisor verification and guarded terminal transitions."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, features, leases, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import lane, lifecycle, recovery, terminal, verification, verification_status
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch import verification_process as procs
from agent_cli.dispatch.state import file as state_file
from agent_cli.dispatch.state import load, save
from agent_cli.dispatch.verification_store import Store
from dispatch_helpers import DEAD_PID, _new, _set_alpha_dispatch, droot, envelope  # noqa: F401
from verification_fixtures import config, fixture  # noqa: F401 — `fixture` is a pytest fixture


@pytest.fixture
def ready(droot: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)
    d = _new(droot)
    ret = envelope()
    ret["session_id"] = d["session_id"]
    ret["structured_output"]["nonce"] = d["nonce"]
    d.update(
        state="running",
        supervisor_pid=DEAD_PID,
        stop_reason="result",
        exit_code=0,
        last_result=ret,
        worker_cleanup_pending=False,
    )
    save(droot, d)
    return droot, d


@pytest.mark.skipif(sys.platform != "win32", reason="the jobless-worker fallback is Windows only")
@pytest.mark.parametrize("supervised", [False, True])
def test_a_cancel_ends_a_jobless_worker_by_pid_only_under_a_live_supervisor(
    ready: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, supervised: bool
):
    """With its supervisor dead, the recorded worker pid may since name an unrelated process,
    so only a named verifier job is ended and the worker's cleanup stays pending."""
    from agent_cli import win_tree
    from agent_cli.dispatch import verification_win

    root, d = ready
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    killed: list[int] = []

    def kill_tree(pid: int) -> bool:
        killed.append(pid)
        worker.kill()
        worker.wait()
        return True

    monkeypatch.setattr(verification_win, "terminate_named", lambda pid: False)
    monkeypatch.setattr(win_tree, "kill_tree", kill_tree)
    try:
        d.update(claude_pid=worker.pid, supervisor_pid=os.getpid() if supervised else DEAD_PID)
        save(root, d)
        gate.settle(root, d["id"], cancel=True, timeout=0)
        after = load(root, d["id"])
        if supervised:
            assert killed == [worker.pid]
            assert after["worker_cleanup_confirmed"] and after["claude_pid"] is None
        else:
            assert killed == [] and worker.poll() is None
            assert after["worker_cleanup_pending"] and not after.get("worker_cleanup_confirmed")
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def outcome(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    return dispatch.evaluate(root, d, repos.load_registry(root).require(d["repo"]))


def finish(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    return terminal.finalize(root, d, features.resolve(root, str(d["feature"])), outcome(root, d))


# How long a controlled verifier gets to reach its barrier. Unloaded it takes about 0.5 s (1.4 s
# with 32 xdist workers on one Mac), yet a loaded macOS runner once passed 12 s. The verifier's
# own deadline was 12 s too, so its guardian would have given up as the barrier did: the verifier
# timeouts below sit past the barrier, and `running` reports an `execute` that ended first.
BARRIER_S = 60


LOG_TAIL = 2000


def diagnose(root: Path, dispatch_id: str) -> str:
    """Dispatch state, assessment, lane holders and the verifier's own evidence, read now so a
    failure keeps them after the temporary checkout is gone."""
    lines: list[str] = []

    def part(label: str, produce) -> None:
        try:
            lines.append(f"{label}: {produce()}")
        except Exception as exc:  # noqa: BLE001 — a diagnostic never hides the failure it explains
            lines.append(f"{label}: unavailable ({type(exc).__name__}: {exc})")

    loaded: list[dict[str, Any]] = []
    keys = ("state", "stop_reason", "attempt", "terminal_committed", "error", "verification_wait")

    def record() -> dict[str, Any]:
        loaded.append(load(root, dispatch_id))
        return {k: loaded[0].get(k) for k in keys}

    part("dispatch", record)
    d = loaded[0] if loaded else {}
    part("result", lambda: loaded[0].get("verification_result") if loaded else "no record")
    if loaded:
        part("assessment", lambda: gate.acceptance(root, d))
        part("requires_lease", lambda: gate.requires_lease(root, d))
    part(
        "lane holders",
        lambda: {
            lease["area"]: lease.get("session_id")
            for lease in leases.list_all(root)
            if str(lease.get("area", "")).lower().startswith(("verify:", "repo-slot:"))
        },
    )

    def run_of() -> tuple[Any, str, Path]:
        value = gate.metadata(d)
        if value is None:
            raise LookupError("no verification metadata")
        binding, mode = value
        return binding, mode, root / lane.run_area(binding)

    def status() -> str:
        binding, mode, run = run_of()
        found = verification.load_status(root, binding)
        return (
            f"mode={mode} dir={run} evidence={found.evidence and found.evidence.state} "
            f"lifecycle={found.lifecycle} active={found.active} cleanup_confirmed={found.cleanup_confirmed}"
        )

    def lifecycle_file() -> dict[str, Any]:
        store = Store(root, run_of()[0])
        try:
            return store.load_lifecycle()
        finally:
            store.close()

    def evidence() -> str:
        store = Store(root, run_of()[0])
        try:
            try:
                life = store.load_lifecycle()
            except Exception:  # noqa: BLE001 — evidence is still readable without it
                life = {"command_index": None}
            found = verification_status.latest(store, life)
            return str(found and found.state)
        finally:
            store.close()

    def output() -> str:
        log = run_of()[2] / "output.log"
        return (
            repr(log.read_bytes()[-LOG_TAIL:].decode("utf-8", errors="replace")) if log.exists() else "None"
        )

    if loaded:
        part("verifier status", status)
        part("verifier lifecycle", lifecycle_file)
        part("verifier evidence", evidence)
        part("verifier output tail", output)
    return "\n".join(lines)


def wait_for(predicate, timeout=BARRIER_S, ended=lambda: None, explain=None):
    deadline = time.monotonic() + timeout
    detail = lambda: f"\n{explain()}" if explain else ""  # noqa: E731
    while time.monotonic() < deadline:
        if predicate():
            return
        if (why := ended()) is not None:
            pytest.fail(f"fixture ended before its controlled barrier: {why}{detail()}")
        time.sleep(0.02)
    pytest.fail(f"fixture did not reach its controlled barrier{detail()}")


def ended_early(future: Future[None], root: Path, dispatch_id: str) -> str | None:
    if not future.done():
        return None
    if (error := future.exception()) is not None:
        return f"gate.execute raised {error!r}"
    # `diagnose` reports the record, result, wait reason and verifier phase; a record it cannot read
    # must not replace this answer.
    return f"gate.execute returned {future.result()!r}"


@contextmanager
def running(ready):
    root, d = ready
    marker, release = root / "verifier-started", root / "verifier-release"
    command = (
        f"touch {shlex.quote(str(marker))}; while ! test -f {shlex.quote(str(release))}; do sleep .02; done"
    )
    _set_alpha_dispatch(
        root,
        verify=[{"command": command, "timeout_seconds": 2 * BARRIER_S}],
        verify_timeout_seconds=3 * BARRIER_S,
    )
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(gate.execute, root, d["id"], d["attempt"])
        try:
            wait_for(
                marker.exists,
                ended=lambda: ended_early(future, root, d["id"]),
                explain=lambda: diagnose(root, d["id"]),
            )
        except BaseException as failure:
            release.touch()  # the barrier failure stays primary; cleanup trouble is a note
            wait([future], timeout=BARRIER_S)
            try:
                future.result(timeout=0)
            except Exception as cleanup:  # noqa: BLE001 — cleanup must not replace the diagnostic
                failure.add_note(f"verifier cleanup failed: {cleanup!r}")
            raise
        try:
            yield root, load(root, d["id"])
        finally:
            release.touch()
            wait([future], timeout=BARRIER_S)
        future.result(timeout=0)


@pytest.mark.parametrize("early", ["raises", "returns"])
def test_a_verifier_future_that_ends_early_fails_promptly_with_state(ready, monkeypatch, early):
    def execute(*args):
        if early == "raises":
            raise RuntimeError("verifier blew up")

    monkeypatch.setattr(gate, "execute", execute)
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception) as info:
        with running(ready):
            pytest.fail("barrier should not be reached")
    assert time.monotonic() - started < 5
    text = str(info.value)
    assert ("raised RuntimeError('verifier blew up')" if early == "raises" else "returned None") in text
    for label in ("dispatch:", "assessment:", "lane holders:", "verifier status:"):
        assert label in text
    assert "repo-slot:alpha/0" in text


def test_diagnose_names_the_wait_reason_and_the_verifier_evidence(ready):
    root, d = ready
    with running(ready) as (_, live):
        text = diagnose(root, d["id"])
        assert "incomplete" in text
        assert "verifier activity or cleanup is unconfirmed" in text
        assert "requires_lease: True" in text
        assert f"dir={root / lane.run_area(gate.metadata(live)[0])}" in text  # type: ignore[index]
    d = load(root, d["id"])
    d["verification_wait"] = {"attempt": d["attempt"], "reason": "waited for a lane", "outcome": "failed"}
    d.pop("verification")
    save(root, d)
    after = diagnose(root, d["id"])
    assert "waited for a lane" in after and "no verification metadata" in after


def test_diagnose_keeps_each_readable_fragment_when_another_is_unreadable(ready):
    root, d = ready
    with running(ready) as (_, live):
        run = root / lane.run_area(gate.metadata(live)[0])  # type: ignore[index]
        log = run / "output.log"
        log.write_text("LOG-SENTINEL")
        lifecycle_file = run / "lifecycle.json"
        good = lifecycle_file.read_text()
        lifecycle_file.write_text("{malformed")
        broken_life = diagnose(root, d["id"])
        assert "LOG-SENTINEL" in broken_life and "verifier status: unavailable" in broken_life
        lifecycle_file.write_text(good)
        log.unlink()
        log.mkdir()
        broken_log = diagnose(root, d["id"])
        assert "verifier output tail: unavailable" in broken_log
        assert "verifier status: mode=run" in broken_log and "lifecycle=" in broken_log
        log.rmdir()


@pytest.mark.parametrize("broken", ["terminal.json", "lifecycle.json"])
def test_diagnose_keeps_verifier_evidence_and_lifecycle_apart(ready, broken):
    root, d = ready
    with running(ready) as (_, live):
        binding = gate.metadata(live)[0]  # type: ignore[index]
        run = root / lane.run_area(binding)
        broken_file = run / broken
        good = broken_file.read_text() if broken_file.exists() else None
        broken_file.write_text("{malformed")
        (run / "output.log").write_text("LOG-SENTINEL")
        try:
            text = diagnose(root, d["id"])
        finally:
            if good is None:
                broken_file.unlink()
            else:
                broken_file.write_text(good)
        assert "LOG-SENTINEL" in text and "verifier status: unavailable" in text
        if broken == "lifecycle.json":
            assert "verifier evidence: unavailable" not in text and "verifier evidence:" in text
            assert "verifier lifecycle: unavailable" in text
        else:
            assert "verifier lifecycle: unavailable" not in text and "phase" in text
            assert "verifier evidence: unavailable" in text


@pytest.mark.parametrize("settles_during_release", [False, True])
@pytest.mark.parametrize("cleanup", ["raises", "returns"])
def test_a_barrier_timeout_keeps_the_diagnostic_failure_when_cleanup_fails(
    ready, monkeypatch, cleanup, settles_during_release
):
    root, d = ready
    release = root / "verifier-release"
    ended = threading.Event()

    def execute(*args):
        try:
            while not release.exists():
                time.sleep(0.02)
            if cleanup == "raises":
                raise RuntimeError("cleanup blew up")
        finally:
            ended.set()

    real_touch = Path.touch

    def touch(self, *args, **kwargs):
        real_touch(self, *args, **kwargs)
        if settles_during_release and self == release:
            assert ended.wait(5)
            time.sleep(0.05)

    original = wait_for
    monkeypatch.setattr(Path, "touch", touch)
    monkeypatch.setattr(gate, "execute", execute)
    monkeypatch.setitem(globals(), "wait_for", lambda *a, **k: original(*a, **{**k, "timeout": 0.3}))
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception) as info:
        with running(ready):
            pytest.fail("barrier should not be reached")
    assert time.monotonic() - started < 5
    text = str(info.value)
    assert "fixture did not reach its controlled barrier" in text and "dispatch:" in text
    if cleanup == "raises":
        assert any("cleanup blew up" in note for note in getattr(info.value, "__notes__", []))


def test_diagnose_survives_an_unreadable_dispatch_record_and_keeps_the_original_failure(ready, monkeypatch):
    root, d = ready
    record = state_file(root, d["id"], ".json")

    def execute(*args):
        record.write_text("{malformed")
        return "ORIGINAL-RESULT"

    monkeypatch.setattr(gate, "execute", execute)
    with pytest.raises(pytest.fail.Exception) as info:
        with running(ready):
            pytest.fail("barrier should not be reached")
    text = str(info.value)
    assert "returned 'ORIGINAL-RESULT'" in text
    assert "dispatch: unavailable" in text and "lane holders:" in text


def test_done_and_collect_wait_for_a_live_verifier_and_preserve_the_lease(ready):
    """Neither a finish nor a collect infers success while the verifier runs; once it has, the
    collect accepts and releases the lease (a finish after the verifier: the finalizer tests)."""
    with running(ready) as (root, d):
        current = finish(root, d)
        assert current["state"] == "running"
        assert leases.get(root, "repo-slot:alpha/0") is not None
        assert not current.get("terminal_committed")
        assert recovery.collect(root, dispatch_id=d["id"])[0]["state"] == "running"
        assert leases.get(root, "repo-slot:alpha/0") is not None
        assert gate.requires_lease(root, load(root, d["id"]))
    assert recovery.collect(root, dispatch_id=d["id"])[0]["state"] == "done"
    accepted = load(root, d["id"])
    assert accepted["verification_result"]["status"] == "verified"
    assert leases.get(root, "repo-slot:alpha/0") is None


def test_cancel_after_worker_stop_cleans_verifier_and_wins(ready):
    with running(ready) as (root, d):
        assert d["stop_reason"] == "result"
        cancelled = recovery.cancel(root, dispatch_id=d["id"])
        assert cancelled["state"] == "cancelled" and cancelled["terminal_committed"]
        value = gate.metadata(cancelled)
        assert value is not None
        binding, _ = value
        assert verification.load_status(root, binding).cleanup_confirmed
        assert leases.get(root, "repo-slot:alpha/0") is None
    assert finish(root, d)["state"] == "cancelled"


def test_a_malformed_or_dead_holder_lease_refuses_take_wait_release_until_cleanup(ready):
    with running(ready) as (root, d):
        path = root / ".agent/leases/repo-slot_alpha~0.json"
        original = path.read_text()
        try:
            path.write_text("{malformed")
            with pytest.raises(AgentError, match="malformed"):
                leases.take(root, "repo-slot:alpha/0", session_id="peer", pid=os.getpid())
            with pytest.raises(AgentError, match="malformed"):
                leases.release(root, "repo-slot:alpha/0", "dispatch:" + d["id"])
        finally:
            path.write_text(original)
        lease = leases.get(root, "repo-slot:alpha/0")
        assert lease is not None
        lease["pid"] = DEAD_PID
        (root / ".agent/leases/repo-slot_alpha~0.json").write_text(json.dumps(lease))
        for method in (leases.take, leases.wait):
            kw: dict = {"timeout": 0} if method is leases.wait else {}
            with pytest.raises(AgentError, match="held by"):
                method(root, "repo-slot:alpha/0", session_id="peer", pid=os.getpid(), **kw)
        assert not leases.release(root, "repo-slot:alpha/0", "dispatch:" + d["id"])
    assert leases.take(root, "repo-slot:alpha/0", session_id="peer", pid=os.getpid())["taken"]


def test_cancel_before_terminal_commit_wins_once_and_late_cancel_refuses(ready, monkeypatch):
    root, d = ready
    gate.execute(root, d["id"], 1)
    entered, release = threading.Event(), threading.Event()
    original = terminal.terminal_guard

    @contextmanager
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        with original(*args, **kwargs) as current:
            yield current

    monkeypatch.setattr(terminal, "terminal_guard", paused)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(finish, root, d)
        assert entered.wait(5)
        with gate.record_guard(root, d["id"]) as current:
            current.update(state="cancelled", cancel_requested="before commit")
            save(root, current)
        release.set()
        result = future.result(timeout=10)
    assert result["state"] == "cancelled"
    assert sum(row.get("from") == "worker" for row in result["thread"]) == 1
    with pytest.raises(AgentError, match="nothing to cancel"):
        recovery.cancel(root, dispatch_id=d["id"])


def test_two_finalizers_have_one_terminal_side_effect(ready, monkeypatch):
    root, d = ready
    gate.execute(root, d["id"], 1)
    barrier = threading.Barrier(2)
    effects = []
    monkeypatch.setattr(terminal, "notify", lambda *args: effects.append("notify"))

    def caller():
        barrier.wait(timeout=5)
        return finish(root, d)

    with ThreadPoolExecutor(2) as pool:
        results = [f.result(timeout=12) for f in [pool.submit(caller), pool.submit(caller)]]
    assert [r["state"] for r in results] == ["done", "done"]
    assert effects == ["notify"]
    assert sum(row.get("from") == "worker" for row in load(root, d["id"])["thread"]) == 1
    with pytest.raises(AgentError, match="nothing to cancel"):
        recovery.cancel(root, dispatch_id=d["id"])


@pytest.mark.parametrize("change", ["attempt", "config", "sha", "malformed", "missing"])
def test_stale_missing_or_malformed_evidence_never_accepts(ready, change):
    root, d = ready
    gate.execute(root, d["id"], 1)
    d = load(root, d["id"])
    if change == "attempt":
        d["attempt"] += 1
    elif change == "config":
        _set_alpha_dispatch(root, verify=[{"command": "false", "timeout_seconds": 3}])
    elif change == "sha":
        (Path(d["worktree"]) / "README.md").write_text("changed after verification")
    elif change == "malformed":
        d["verification"]["binding"]["attempt"] = True
    else:
        d.pop("verification")
    save(root, d)
    assert finish(root, d)["state"] != "done"
    assert recovery.collect(root, dispatch_id=d["id"])[0]["state"] != "done"
    if change == "malformed":
        assert leases.get(root, "repo-slot:alpha/0") is not None


def test_collect_before_verification_never_runs_commands(ready, monkeypatch):
    root, d = ready
    monkeypatch.setattr(verification, "prepare", lambda *a, **kw: pytest.fail("collect reran checks"))
    assert recovery.collect(root, dispatch_id=d["id"])[0]["state"] == "blocked"
    assert load(root, d["id"])["verification_result"]["status"] == "unverified"


def test_explicit_exemption_does_not_require_clean_tracked_tree(ready):
    root, d = ready
    d["no_checks"] = True
    save(root, d)
    (Path(d["worktree"]) / "README.md").write_text("pre-existing review dirt")
    gate.execute(root, d["id"], 1)
    result = finish(root, d)
    assert result["state"] == "done"
    assert result["verification_result"]["status"] == "exempt"
    assert "verification exempt" in result["result"]["notes"]


def test_unconfigured_worker_done_is_blocked(ready):
    root, d = ready
    path = root / "agent.yaml"
    import yaml

    registry = yaml.safe_load(path.read_text())
    for repo in registry["repos"]:
        repo.get("dispatch", {}).pop("verify", None)
        repo.get("dispatch", {}).pop("verify_timeout_seconds", None)
    path.write_text(yaml.safe_dump(registry))
    gate.execute(root, d["id"], 1)
    assert finish(root, d)["state"] == "blocked"


def test_resume_refuses_before_worktree_observation_or_recreation(ready, monkeypatch):
    root, d = ready
    d["state"] = "blocked"
    save(root, d)
    leases.release(root, "repo-slot:alpha/0", "dispatch:" + d["id"])
    leases.take(root, "repo-slot:alpha/0", session_id="peer", pid=os.getpid())
    monkeypatch.setattr(lifecycle, "ensure_worktree", lambda *a: pytest.fail("created before lease"))
    monkeypatch.setattr(repos, "git_or_none", lambda *a: pytest.fail("read branch before lease"))
    with pytest.raises(AgentError, match="held by"):
        dispatch.resume(root, dispatch_id=d["id"], answer="continue")


def test_other_live_lease_holder_blocks_final_done(ready):
    root, d = ready
    gate.execute(root, d["id"], 1)
    assert leases.release(root, "repo-slot:alpha/0", "dispatch:" + d["id"])
    leases.take(root, "repo-slot:alpha/0", session_id="peer", pid=os.getpid())
    result = finish(root, d)
    assert result["state"] == "blocked" and "lease changed" in result["error"]
    lease = leases.get(root, "repo-slot:alpha/0")
    assert lease is not None and lease["session_id"] == "peer"


def test_cleanup_force_cannot_remove_active_verification(ready):
    from agent_cli.dispatch.worktrees import cleanup

    with running(ready) as (root, d):
        with gate.record_guard(root, d["id"]) as current:
            current["state"] = "cancelled"
            save(root, current)
        with pytest.raises(AgentError, match="active|cleanup"):
            cleanup(root, feature="1", force=True)
        assert Path(d["worktree"]).exists()


def test_supervisor_exception_settles_verifier_before_generic_reap(ready, monkeypatch):
    root, d = ready
    d["state"] = "queued"
    save(root, d)
    marker = root / "command-started"
    command = f"touch {shlex.quote(str(marker))}; sleep 20"
    _set_alpha_dispatch(root, verify=[{"command": command, "timeout_seconds": 25}], verify_timeout_seconds=30)
    handles = []

    def launch(*args):
        with gate.record_guard(root, d["id"]) as current:
            current.update(state="running", supervisor_pid=os.getpid())
            save(root, current)
            return current

    def fail_wait(handle):
        handles.append(handle)
        wait_for(marker.exists)
        raise RuntimeError("injected after verifier starts")

    reaped = []

    def reap(*args):
        value = gate.metadata(load(root, d["id"]))
        assert value is not None
        binding, _ = value
        assert verification.load_status(root, binding).cleanup_confirmed
        reaped.append(True)

    monkeypatch.setattr(dispatch.supervisor, "_launch_attempt", launch)
    monkeypatch.setattr(dispatch.supervisor, "_reap_dispatch_tree", reap)
    monkeypatch.setattr(verification, "wait", fail_wait)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            dispatch.run(root, dispatch_id=d["id"])
        assert reaped and load(root, d["id"])["state"] == "failed"
        assert leases.get(root, "repo-slot:alpha/0") is None
    finally:
        for handle in handles:
            handle.process.stdin.close()
            handle.process.wait(timeout=5)


def _run_verifier(root: Path, d: dict, *, command: str, env_allow: list[str] | None = None):
    """Publish and run one verifier command against `d`'s repo config, then return its
    `CommandOutcome`. `gate.execute` blocks until the guardian finishes (same as the direct
    `gate.execute(root, d["id"], 1)` calls above), so the caller sees the real exit code."""
    fields: dict = {"verify": [{"command": command, "timeout_seconds": 5}], "verify_timeout_seconds": 8}
    if env_allow is not None:
        fields["env_allow"] = env_allow
    _set_alpha_dispatch(root, **fields)
    gate.execute(root, d["id"], d["attempt"])
    value = gate.metadata(load(root, d["id"]))
    assert value is not None
    binding, mode = value
    assert mode == "run"
    status = verification.load_status(root, binding)
    assert status.evidence is not None
    assert len(status.evidence.commands) == 1
    return status.evidence.commands[0]


def test_verifier_command_resolves_on_manager_granted_path(ready, monkeypatch, tmp_path):
    """Regression for the defect this feature fixes: `verification_gate.execute` used to call
    `verification.prepare()` with no `environment`, so `prepare()` fell back to
    `verification_process.BASE_ENV` (`{"PATH": os.defpath, ...}` — `/usr/bin:/bin:/usr/sbin:
    /sbin` on macOS) regardless of what the manager's own `PATH` held. A repo's toolchain
    (`uv`, `npm`, ...) living anywhere else, e.g. Homebrew's `/opt/homebrew/bin`, made
    every verification command fail with `command not found` no matter what the manager could
    itself run. This fails against the old code: the script lives outside `os.defpath` and
    the old fallback never saw the manager's `PATH` at all."""
    root, d = ready
    bin_dir = tmp_path / "verifier-bin"
    bin_dir.mkdir()
    script = bin_dir / "verify-toolchain"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    outcome = _run_verifier(root, d, command="verify-toolchain")
    assert outcome.outcome == "exited"
    assert outcome.exit_code == 0


def test_verifier_env_drops_unallowlisted_ssh_and_claude_variables_and_grants_named_ones(ready, monkeypatch):
    """A manager variable and SSH_AUTH_SOCK are dropped by default, SSH_AUTH_SOCK is granted when
    the repo names it, and a CLAUDE_ variable is dropped even when an env_allow pattern matches."""
    root, d = ready
    monkeypatch.setenv("MANAGER_SECRET_SENTINEL", "should-not-reach-verifier")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fixture-agent.sock")
    monkeypatch.setenv("CLAUDE_FIXTURE_TOKEN", "should-not-reach-verifier")
    for command, env_allow in (
        ('test -z "$MANAGER_SECRET_SENTINEL"', None),
        ('test -z "$SSH_AUTH_SOCK"', None),
        ('test -n "$SSH_AUTH_SOCK"', ["SSH_AUTH_SOCK"]),
        ('test -z "$CLAUDE_FIXTURE_TOKEN"', ["CLAUDE_*"]),
    ):
        outcome = _run_verifier(root, d, command=command, env_allow=env_allow)
        assert (outcome.outcome, outcome.exit_code) == ("exited", 0), command


#: Past the 4096-byte Windows pipe buffer and inside one write buffer, like the temp-root test's
#: 4.4-4.7 KB command request.
REQUEST_PADDING = "x" * 4500


def _one_command(fixture, monkeypatch, *, before_command_send=None):
    """Run `_command` on real role processes, recording each launch, send and control close."""
    root, worktree, sha = fixture
    prepared = verification.prepare(
        root, worktree, dispatch_id="001-01", attempt=1, final_sha=sha, config=config("printf ok")
    )
    launched: dict[str, subprocess.Popen] = {}
    events: list[tuple[str, str]] = []
    sizes: dict[str, int] = {}
    closes: list[tuple[str, BaseException | None]] = []
    real_launch, real_send, real_close = procs.launch, procs.send, procs.close_control

    def role(process):
        return next(name for name, launch in launched.items() if launch is process)

    def launch(name, **kwargs):
        launched[name] = real_launch(name, **kwargs)
        events.append(("launch", name))
        return launched[name]

    def send(process, value):
        events.append(("send", role(process)))
        sizes[role(process)] = len(json.dumps(value)) + 1
        if role(process) == "command" and before_command_send is not None:
            before_command_send(process)
        real_send(process, value)

    def close_control(process):
        try:
            real_close(process)
        except BaseException as exc:
            closes.append((role(process), exc))
            raise
        closes.append((role(process), None))

    monkeypatch.setattr(procs, "launch", launch)
    monkeypatch.setattr(procs, "send", send)
    monkeypatch.setattr(procs, "close_control", close_control)
    monkeypatch.setattr(procs, "parent_gone", lambda: False)  # pytest owns this process's stdin
    store = Store(prepared.root, prepared.binding)
    life = {k: v for k, v in store.load_lifecycle().items() if k not in {"binding", "schema_version"}}
    environment = {**prepared.environment, "VERIFY_PADDING": REQUEST_PADDING}
    request = {"environment": environment, "worktree": str(prepared.worktree), **prepared.tools}
    try:
        with (root / "output.log").open("wb") as log:
            result = verification._command(
                store, life, request, 0, "printf ok", 10.0, time.monotonic() + 15, log
            )
    finally:
        store.close()
    return result, life, launched, events, sizes, closes


def test_a_command_request_past_the_pipe_buffer_does_not_hold_back_the_watchdog(fixture, monkeypatch):
    """A command request past the 4096-byte Windows pipe buffer blocks its send until the command
    process has cold-started and read it. The watchdog used to launch only after that send, so
    the two cold starts ran one after the other inside the command's deadline, and under load the
    temp-root verifier test ran out of it. The watchdog now launches and is sent its request
    first, so its cold start overlaps the command's."""
    result, life, _, events, sizes, _ = _one_command(fixture, monkeypatch)
    assert sizes["command"] > 4096
    assert events.index(("launch", "watchdog")) < events.index(("send", "command"))
    assert events.index(("send", "watchdog")) < events.index(("send", "command"))
    assert (result.outcome, result.exit_code) == ("exited", 0)
    assert life["cleanup_confirmed"] is True


@pytest.mark.parametrize("certified", [True, False])
def test_a_send_broken_by_the_ended_job_still_runs_every_cleanup_step(fixture, monkeypatch, certified):
    """The watchdog can end the command's job while the command's request still waits in the
    pipe. The send then fails on a broken pipe and leaves the request buffered, so closing the
    control pipe raises again, and that second error must not skip the rest of cleanup. Here the
    job is ended just before the send, as the watchdog would end it."""
    killed: list[int] = []
    released: list[int] = []
    real_kill, real_release = procs.kill_group, procs.release
    monkeypatch.setattr(procs, "kill_group", lambda pgid: killed.append(pgid) or real_kill(pgid))
    monkeypatch.setattr(procs, "release", lambda pid: released.append(pid) or real_release(pid))
    if not certified:
        monkeypatch.setattr(verification, "_cleanup_record", lambda store, life: None)

    def end_the_job(child):
        assert real_kill(child.pid)

    result, life, launched, _, _, closes = _one_command(fixture, monkeypatch, before_command_send=end_the_job)
    child, watchdog = launched["command"], launched["watchdog"]
    assert closes[0][0] == "command" and isinstance(closes[0][1], OSError), "the broken send never engaged"
    assert [name for name, _ in closes] == ["command", "watchdog"]
    assert watchdog.stdout is not None and child.stdout is not None
    assert watchdog.returncode == 0 and watchdog.stdout.closed
    assert child.returncode is not None and child.stdout.closed
    assert released == [child.pid]
    assert killed == ([] if certified else [child.pid])
    assert (result.outcome, result.exit_code) == ("error", None)
    assert life["phase"] == "cleanup"
    assert life["cleanup_confirmed"] is certified
    assert (life.get("reason") == "cleanup_unconfirmed") is not certified


def test_collect_and_supervisor_finalization_commit_once(ready, monkeypatch):
    root, d = ready
    gate.execute(root, d["id"], 1)
    barrier = threading.Barrier(2)
    effects = []
    monkeypatch.setattr(terminal, "notify", lambda *args: effects.append(True))

    def supervisor():
        barrier.wait(5)
        return finish(root, d)

    def collector():
        barrier.wait(5)
        return recovery.collect(root, dispatch_id=d["id"])

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(supervisor), pool.submit(collector)]
        for future in futures:
            future.result(timeout=15)
    assert effects == [True]
    assert load(root, d["id"])["state"] == "done"
