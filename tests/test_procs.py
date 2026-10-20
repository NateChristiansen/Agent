"""Process ownership and reaping.

The failure these hold: a setup step hit its timeout, `subprocess.run(shell=True, timeout=...)`
killed only the `/bin/sh`, and a download it had started ran on for days holding a machine-wide
lock.

The tests that plant real processes plant their own and kill their own. The autouse
`_no_real_reaping` fixture empties `procs.snapshot`, so nothing here reads the machine's real
process table unless a test deliberately restores it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from agent_cli import core, proc_tags, procs

REAL_SNAPSHOT = procs.snapshot  # read at import, before conftest's autouse stub replaces it


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) is CTRL_C_EVENT there and interrupted the whole test run.
        return core.is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers signal 0; ask ps whether it is really running
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def test_windows_reap_selection_reports_unreadable_descendant(monkeypatch: pytest.MonkeyPatch):
    owned = procs.Proc(2252, 728, 2252, "1", "python", dispatch="ours", started="birth")
    unreadable = procs.Proc(2253, 2252, 2253, "1", "python", started="birth-2", tag_error="AccessDenied")
    unrelated = procs.Proc(2254, 800, 2254, "1", "python", started="birth-3", tag_error="AccessDenied")
    monkeypatch.setattr(procs, "snapshot", lambda: [owned, unreadable, unrelated])
    monkeypatch.setattr(procs, "process_started", lambda pid: "birth-2" if pid == 2253 else "")

    assert procs.select_windows_reap(dispatch="ours") == ([owned], [unreadable])


def test_windows_cancel_reports_unverified_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from agent_cli.dispatch import driver

    unknown = procs.Proc(2253, 728, 2253, "1", "python", started="birth", tag_error="AccessDenied")
    monkeypatch.setattr(procs, "snapshot", lambda: [unknown])
    monkeypatch.setattr(procs, "process_started", lambda pid: "birth")
    monkeypatch.setattr(driver, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(procs, "reap", lambda root, targets: pytest.fail("unverified PID must not be reaped"))

    assert driver.kill_group(728, root=tmp_path, dispatch_id="ours") == [2253]

    monkeypatch.setattr(procs, "process_started", lambda pid: "")
    assert driver.kill_group(728, root=tmp_path, dispatch_id="ours") == []


# ------------------------------------------------------------------ run_bounded


def test_run_bounded_returns_output_like_subprocess_run():
    result = procs.run_bounded(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        timeout=10,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_a_timeout_kills_the_grandchild_too(tmp_path: Path):
    """A regression. The shell starts a grandchild, writes its pid, then blocks. Plain
    `subprocess.run(shell=True, timeout=...)` would kill only the shell and leave the grandchild
    running — which is exactly the ten-day orphan."""
    pidfile = tmp_path / "grandchild.pid"
    if sys.platform == "win32":
        code = (
            "import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(61)']); "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        cmd = [sys.executable, "-c", code]
    else:
        cmd = f"sleep 61 & echo $! > {pidfile}; sleep 60"
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run_bounded(cmd, shell=isinstance(cmd, str), timeout=1)
    grandchild = int(pidfile.read_text().strip())
    assert _wait_gone(grandchild), f"grandchild {grandchild} outlived the timeout"


def test_the_command_gets_no_stdin():
    """A command that waits on input gets EOF instead of hanging. That is how a probe of an MCP
    server hung on 2026-09-18: it started serving on stdio and waited forever."""
    started = time.monotonic()
    result = procs.run_bounded([sys.executable, "-c", "import sys; sys.stdin.read()"], timeout=10)
    assert result.returncode == 0
    assert time.monotonic() - started < 5


def test_worktree_setup_timeout_leaves_nothing_behind(tmp_path: Path, monkeypatch):
    """The same regression through the real call site, `run_worktree_setup`."""
    from agent_cli import repos as repos_mod
    from agent_cli.dispatch import worktrees

    monkeypatch.setattr(worktrees, "WORKTREE_SETUP_TIMEOUT_S", 1.0)
    pidfile = tmp_path / "grandchild.pid"
    if sys.platform == "win32":
        code = (
            "import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(61)']); "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        setup = subprocess.list2cmdline([sys.executable, "-c", code])
    else:
        setup = f"sleep 61 & echo $! > {pidfile}; sleep 60"
    repo = repos_mod.Repo(
        name="x",
        short="x",
        path="x",
        remote="r",
        dispatch={"setup": [setup]},
    )
    (out,) = worktrees.run_worktree_setup(tmp_path, repo, tmp_path)
    assert out["exit"] == -1 and "timed out" in out["error"]
    grandchild = int(pidfile.read_text().strip())
    assert _wait_gone(grandchild), f"setup's grandchild {grandchild} outlived the timeout"


# ------------------------------------------------------------------ ownership tags


def test_parse_tags_finds_both_by_name():
    line = (
        "node server.js --flag PATH=/usr/bin CLAUDE_CODE_SESSION_ID=abc-123 "
        "AGENT_DISPATCH_ID=003-01 HOME=/Users/x"
    )
    assert proc_tags.parse_tags(line) == {
        procs.TAG_SESSION: "abc-123",
        procs.TAG_DISPATCH: "003-01",
    }


def test_parse_tags_does_not_match_a_longer_name():
    assert proc_tags.parse_tags("x NOT_AGENT_DISPATCH_ID=1 XCLAUDE_CODE_SESSION_ID=2 XAGENT_ROOT=/r") == {}


def test_parse_tags_reads_a_root_with_spaces_up_to_the_next_variable():
    line = "python -m x AGENT_ROOT=/Users/a b/My Vault AGENT_DISPATCH_ID=001-01 HOME=/Users/a b"
    assert proc_tags.parse_tags(line)[procs.TAG_ROOT] == "/Users/a b/My Vault"
    assert proc_tags.parse_tags("python AGENT_ROOT=/v/last")[procs.TAG_ROOT] == "/v/last"


def test_windows_snapshot_uses_environment_tags_and_parent_age(monkeypatch: pytest.MonkeyPatch):
    from agent_cli import win_snapshot

    def row(pid: int, ppid: int, created: float, env: dict[str, str]):
        return types.SimpleNamespace(
            environ=lambda: env,
            create_time=lambda: created,
            cmdline=lambda: ["python", "child.py"],
        )

    rows = {
        100: row(100, 1, 10, {}),
        101: row(101, 100, 20, {procs.TAG_DISPATCH: "ours", procs.TAG_ROOT: "C:/vault"}),
        102: row(102, 999, 20, {procs.TAG_SESSION: "escaped"}),
    }
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        types.SimpleNamespace(Process=lambda pid: rows[pid], NoSuchProcess=OSError, AccessDenied=OSError),
    )
    monkeypatch.setitem(
        sys.modules,
        "agent_cli.winproc",
        types.SimpleNamespace(
            pid_started=lambda pid: str(pid),
            process_entries=lambda: [(100, 1, "python"), (101, 100, "python"), (102, 999, "python")],
        ),
    )
    snapshot = win_snapshot.snapshot()
    assert [p.pid for p in procs.select(snapshot, dispatch="ours")] == [101]
    assert snapshot[1].started == "101" and not snapshot[1].escaped
    assert snapshot[1].root == "C:/vault"
    assert snapshot[2].escaped


def _p(
    pid: int,
    *,
    ppid: int = 1,
    session: str = "",
    dispatch: str = "",
    command: str = "x",
    started: str = "",
    root: str = "",
) -> procs.Proc:
    return procs.Proc(
        pid=pid,
        ppid=ppid,
        pgid=pid,
        elapsed="00:01",
        command=command,
        session=session,
        dispatch=dispatch,
        started=started,
        root=root,
    )


def test_select_follows_the_ownership_tree():
    snapshot = [
        _p(10, session="mgr-a"),
        _p(11, session="mgr-a", ppid=500),  # still attached to its session
        _p(12, session="mgr-b"),  # another manager's: never ours
        _p(20, dispatch="003-01"),
        _p(21, dispatch="003-02"),
        _p(30, dispatch="003-01", command="/x/bin/agent dispatch run 003-01"),  # a supervisor
    ]
    assert [p.pid for p in procs.select(snapshot, session="mgr-a")] == [10, 11]
    assert [p.pid for p in procs.select(snapshot, session="mgr-a", escaped_only=True)] == [10]
    assert [p.pid for p in procs.select(snapshot, dispatch="003-01")] == [20]


def test_a_supervisor_is_never_selected():
    """Detached on purpose so a worker survives its manager; a supervisor ends its own run."""
    sup = _p(30, session="mgr-a", dispatch="003-01", command="python .venv/bin/agent dispatch run 003-01")
    assert sup.supervisor_of == "003-01"
    assert procs.select([sup], session="mgr-a") == []
    assert procs.select([sup], dispatch="003-01") == []


# ------------------------------------------------------------------ keep-alive and reaping


def _plant() -> subprocess.Popen[bytes]:
    windows = sys.platform == "win32"
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=not windows,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if windows else 0,
    )


def test_keep_records_a_live_process_and_forgets_a_dead_one(tmp_root: Path):
    child = _plant()
    try:
        assert procs.keep(tmp_root, child.pid, "dev server")["kept"] is True
        assert child.pid in procs.kept(tmp_root)
    finally:
        child.kill()
        child.wait()
    assert child.pid not in procs.kept(tmp_root), "a dead pid must not stay protected"


def test_keep_refuses_a_pid_that_does_not_exist(tmp_root: Path):
    assert procs.keep(tmp_root, 999_999)["kept"] is False


def test_reap_kills_targets_and_spares_what_was_kept(tmp_root: Path):
    doomed, spared = _plant(), _plant()
    try:
        procs.keep(tmp_root, spared.pid, "the owner is using this")
        result = procs.reap(
            tmp_root,
            [
                _p(doomed.pid, started=procs.process_started(doomed.pid)),
                _p(spared.pid, started=procs.process_started(spared.pid)),
            ],
            grace=2,
        )
        doomed.wait(timeout=5)
        assert doomed.returncode is not None
        assert spared.poll() is None, "a kept process was reaped"
        assert result["skipped_kept"] == [spared.pid]
    finally:
        for c in (doomed, spared):
            if c.poll() is None:
                c.kill()
                c.wait()


def test_reap_never_touches_its_own_process(tmp_root: Path):
    result = procs.reap(tmp_root, [_p(os.getpid()), _p(os.getppid())], grace=0.1)
    assert result["terminated"] == [] and result["killed"] == []


# ------------------------------------------------------------------ classify, and doctor


def test_classify_finds_orphans_and_spares_live_owners(tmp_root: Path, monkeypatch):
    """A session is live while anything still ATTACHED to it carries its tag — its own MCP servers,
    at least — so a session with only escaped processes left is gone. An escaped process of a live
    owner is not an orphan yet: its owner's own reap will take it."""
    monkeypatch.setattr(procs, "_dispatch_live", lambda root, did, cache: did == "003-01")
    snapshot = [
        _p(10, session="gone"),  # escaped, and nothing attached carries "gone": orphan
        _p(11, session="live"),  # escaped, but its session still has an attached child
        _p(12, session="live", ppid=4000),  # that attached child
        _p(20, dispatch="003-02"),  # escaped, dispatch finished: orphan
        _p(21, dispatch="003-01"),  # escaped, dispatch still running: not yet
        _p(30, dispatch="003-02", command="x/bin/agent dispatch run 003-02"),  # stuck supervisor
    ]
    rows = {r["pid"]: r for r in procs.classify(tmp_root, snapshot)}
    assert [pid for pid, r in rows.items() if r["orphan"]] == [10, 20]
    assert rows[30]["stuck_supervisor"] and not rows[30]["orphan"], "a supervisor is flagged, never reaped"


def test_a_supervisor_finishing_a_cancel_is_exiting_not_stuck(tmp_root: Path, monkeypatch):
    """`cancel` writes `cancelled` before the supervisor has interrupted, finalized and reaped.
    A `procs_list` in that window read `(gone) ... [STUCK SUPERVISOR, escaped]`."""
    from agent_cli.dispatch.state import file
    from agent_cli.ops import harness

    monkeypatch.setenv("AGENT_NOW", "2026-10-14T12:00:10+00:00")
    records = {
        "001-01": {"state": "cancelled", "cancel_requested": "2026-10-14T12:00:07+00:00"},
        "001-02": {"state": "cancelled", "cancel_requested": "2026-10-14T11:50:00+00:00"},
        "001-03": {"state": "done", "finished": "2026-10-14T12:00:05+00:00"},
    }
    for did, record in records.items():
        path = file(tmp_root, did, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": did, **record}), encoding="utf-8")
    snapshot = [
        _p(40, dispatch="001-01", command="/x/bin/agent dispatch run 001-01"),
        _p(41, dispatch="001-02", command="/x/bin/agent dispatch run 001-02"),
        _p(42, dispatch="001-03", command="/x/bin/agent dispatch run 001-03"),
    ]
    rows = {r["pid"]: r for r in procs.classify(tmp_root, snapshot)}
    assert not rows[40]["stuck_supervisor"] and rows[40]["supervisor_exiting"]
    assert not rows[42]["stuck_supervisor"] and rows[42]["supervisor_exiting"]
    assert rows[41]["stuck_supervisor"], "ten minutes after the cancel it is stuck"
    line = harness._proc_line(rows[40])
    assert "STUCK" not in line and "escaped" not in line and "supervisor exiting" in line


def test_doctor_warns_on_an_orphan(tmp_root: Path, run, monkeypatch):
    monkeypatch.setattr(procs, "snapshot", lambda: [_p(10, session="gone")])
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "processes"][-1]
    assert check["level"] == "warn"
    assert "1 orphaned" in check["detail"] and "procs reap" in check["detail"]


def test_doctor_is_quiet_when_nothing_leaked(tmp_root: Path, run):
    r = run(["--json", "doctor"])
    check = [c for c in r.json if c["check"] == "processes"][-1]
    assert check["level"] == "ok"


# ------------------------------------------------------------------ the supervisor's reap


def test_a_dispatch_end_reaps_only_its_own_tree(tmp_root: Path, monkeypatch):
    """Everything tagged with this dispatch in this root, and nothing else: not another
    dispatch's processes, not a manager's, not the supervisor itself, and not a process another
    harness root started under the same dispatch id."""
    from agent_cli.dispatch import supervisor

    other_root = tmp_root.parent / "other-vault"
    other_root.mkdir()
    ours = str(tmp_root)
    alias = tmp_root / ".agent" / ".."  # another spelling of this root still matches
    snapshot = [
        _p(20, dispatch="003-01", root=ours),
        _p(21, dispatch="003-01", ppid=400, root=ours),
        _p(22, dispatch="003-02", root=ours),
        _p(23, session="mgr-a"),
        _p(24, dispatch="003-01", command="/x/bin/agent dispatch run 003-01", root=ours),
        _p(25, dispatch="003-01", root=str(other_root)),
        _p(26, dispatch="003-01", command="python -c main() dispatch run 003-01", root=str(other_root)),
        _p(27, dispatch="003-01"),
        _p(28, dispatch="003-01", root=str(alias)),
    ]
    seen: list[list[int]] = []
    monkeypatch.setattr(procs, "snapshot", lambda: snapshot)
    monkeypatch.setattr(
        procs,
        "reap",
        lambda root, targets, **kw: (
            seen.append([p.pid for p in targets]) or {"terminated": [], "killed": [], "skipped_kept": []}
        ),
    )
    supervisor._reap_dispatch_tree(tmp_root, "003-01")
    assert seen == [[20, 21, 28]]


def test_a_dispatch_end_reap_never_raises(tmp_root: Path, monkeypatch):
    """A backstop must not be the thing that fails a dispatch."""
    from agent_cli.dispatch import supervisor

    def boom():
        raise RuntimeError("ps exploded")

    monkeypatch.setattr(procs, "snapshot", boom)
    supervisor._reap_dispatch_tree(tmp_root, "003-01")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod(0) does not deny reads on Windows, so the unreadable-environ case cannot exist",
)
def test_linux_tags_come_from_proc_environ(tmp_path: Path):
    """Linux has no `ps -E`; an unread tag made every real Linux inventory untrusted (CI)."""
    (tmp_path / "10").mkdir()
    (tmp_path / "10" / "environ").write_bytes(b"PATH=/bin\0AGENT_DISPATCH_ID=001-01\0AGENT_ROOT=/r w\0")
    (tmp_path / "11").mkdir()
    (tmp_path / "11" / "environ").write_bytes(b"HOME=/x\0")
    (tmp_path / "13").mkdir()
    hidden = tmp_path / "13" / "environ"
    hidden.write_bytes(b"AGENT_DISPATCH_ID=001-02\0")
    hidden.chmod(0)
    try:
        read, tags = proc_tags.linux_tags([10, 11, 12, 13], proc=tmp_path)
    finally:
        hidden.chmod(0o600)
    assert read == {10, 11, 13}  # 12 has exited; 13 is another user's and cannot be ours
    assert tags[10] == {procs.TAG_DISPATCH: "001-01", procs.TAG_ROOT: "/r w"}
    assert tags[11] == {} and tags[13] == {}


def test_a_linux_snapshot_reads_tags_without_ps_e(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_ps(args: list[str]) -> str:
        calls.append(args)
        return "  10     1    10 00:01 python worker\n  11    10    10 00:01 sleep 60\n"

    monkeypatch.setattr(procs.sys, "platform", "linux")
    monkeypatch.setattr(procs, "_ps", fake_ps)
    monkeypatch.setattr(procs, "linux_tags", lambda pids: (set(pids), {10: {procs.TAG_DISPATCH: "001-01"}}))
    rows = REAL_SNAPSHOT()
    assert [(p.pid, p.dispatch, p.tag_error) for p in rows] == [(10, "001-01", ""), (11, "", "")]
    assert all("-E" not in args for args in calls)
