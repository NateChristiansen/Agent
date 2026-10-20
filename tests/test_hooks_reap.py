"""`session-reap`: SessionEnd kills what escaped the ending manager session.

Every test feeds the hook its own process table; nothing here reads or signals the machine's
real processes (the autouse `_no_real_reaping` fixture empties `procs.snapshot` otherwise).
"""

import json
from pathlib import Path

from agent_cli import procs


def _p(pid: int, *, ppid: int = 1, session: str = "", dispatch: str = "", command: str = "x") -> procs.Proc:
    return procs.Proc(
        pid=pid, ppid=ppid, pgid=pid, elapsed="00:01", command=command, session=session, dispatch=dispatch
    )


def _record_reaps(monkeypatch, snapshot: list[procs.Proc]) -> list[list[int]]:
    seen: list[list[int]] = []
    monkeypatch.setattr(procs, "snapshot", lambda: snapshot)
    monkeypatch.setattr(
        procs,
        "reap",
        lambda root, targets, **kw: (
            seen.append([p.pid for p in targets]) or {"terminated": [], "killed": [], "skipped_kept": []}
        ),
    )
    return seen


def test_session_reap_takes_only_what_escaped_this_session(tmp_root: Path, run, monkeypatch):
    """Escaped and tagged with this session: reaped. Still attached: left to Claude Code, which
    kills its own children. Another manager's: never ours. A worker's: survives its manager."""
    seen = _record_reaps(
        monkeypatch,
        [
            _p(10, session="mgr-a"),  # escaped, ours
            _p(11, session="mgr-a", ppid=4000),  # still attached: Claude Code's to clean up
            _p(12, session="mgr-b"),  # another live manager's
            _p(13, dispatch="001-01"),  # a worker's: workers outlive their manager
            _p(14, command="/x/bin/agent dispatch run 001-01", dispatch="001-01"),  # a supervisor
        ],
    )
    r = run(["hook", "session-reap"], stdin=json.dumps({"session_id": "mgr-a"}))
    assert r.code == 0
    assert seen == [[10]]


def test_session_reap_without_a_session_id_does_nothing(tmp_root: Path, run, monkeypatch):
    seen = _record_reaps(monkeypatch, [_p(10, session="mgr-a")])
    r = run(["hook", "session-reap"], stdin=json.dumps({}))
    assert r.code == 0
    assert seen == []


def test_session_reap_fails_open(tmp_root: Path, run, monkeypatch):
    """A hook never raises and always exits 0, even when ps itself breaks."""

    def boom():
        raise RuntimeError("ps exploded")

    monkeypatch.setattr(procs, "snapshot", boom)
    r = run(["hook", "session-reap"], stdin=json.dumps({"session_id": "mgr-a"}))
    assert r.code == 0
