"""Claude Code's `statusLine` command. Shows running/queued dispatches
with elapsed time and cost, `question`/`blocked` dispatches waiting on `chat_pending`, and
this session's leases (or every live lease when the stdin JSON carries no session id).

Dispatch records are written directly as raw json here, not run through a real dispatch.
`ops.status_line` only ever reads the file, and a real dispatch needs the fake worker
transport this suite uses elsewhere. The schema is `id`/`short`/`state`/`started`/
`created`/`cost_usd`/`chat_pending`, per `src/agent_cli/dispatch.py`'s `new()`.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from agent_cli import leases as leases_mod
from agent_cli import ops
from agent_cli.core import iso, now

pytestmark = pytest.mark.usefixtures("notes_remote")


def _write_dispatch(root: Path, dispatch_id: str, **fields) -> None:
    record = {
        "id": dispatch_id,
        "feature": 1,
        "repo": "alpha-api",
        "short": "alpha",
        "worktree": str(root / "repos" / "_worktrees" / "alpha" / "001-x"),
        "branch": "agent/001-x",
        "session_id": "worker-session",
        "nonce": "n",
        "state": "queued",
        "attempt": 1,
        "budget_usd": 2.0,
        "started": None,
        "created": iso(now()),
        "cost_usd": 0.0,
        "chat_pending": False,
        "handoff": "features/001-x/handoffs/01-alpha.md",
    }
    record.update(fields)
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def test_running_dispatch_renders_elapsed_and_cost(tmp_root: Path):
    started = iso(now() - timedelta(minutes=5))
    _write_dispatch(tmp_root, "002-01", state="running", started=started, cost_usd=1.2345)
    op = ops.status_line(tmp_root)
    assert op.text == "⚙ 002-01 alpha 5m $1.23"


def test_queued_dispatch_renders(tmp_root: Path):
    _write_dispatch(tmp_root, "002-01", state="queued", started=None, cost_usd=0.0)
    op = ops.status_line(tmp_root)
    assert op.text == "⚙ 002-01 alpha 0m"


def test_a_running_worker_shows_tokens_until_its_dollars_exist(tmp_root: Path):
    """A worker's cost only exists in the SDK's result message, at the end of the
    run, so the line showed $0.00 for a worker that had been going half an hour and read as
    free. The supervisor writes tokens as the worker streams; those show until the dollars
    land."""
    started = iso(now() - timedelta(minutes=32))
    _write_dispatch(tmp_root, "002-01", state="running", started=started, cost_usd=0.0, tokens=1_240_000)
    assert ops.status_line(tmp_root).text == "⚙ 002-01 alpha 32m 1.2M tok"
    _write_dispatch(tmp_root, "002-01", state="running", started=started, cost_usd=4.2, tokens=1_240_000)
    assert ops.status_line(tmp_root).text == "⚙ 002-01 alpha 32m $4.20"


def test_missing_started_falls_back_to_created(tmp_root: Path):
    created = iso(now() - timedelta(minutes=9))
    _write_dispatch(tmp_root, "002-01", state="running", started=None, created=created, cost_usd=0.5)
    op = ops.status_line(tmp_root)
    assert op.text == "⚙ 002-01 alpha 9m $0.50"


def test_a_finished_dispatch_waits_while_it_is_owed(tmp_root: Path):
    """The line reads `live.obligations`, as Needs you, the tab and the
    session-start block do. An unread result, or a branch with work nobody landed or acked,
    waits; a read result with no diff owes nothing (the retired "until acted on" rule)."""
    built = {"commits": [{"sha": "a", "subject": "s"}], "files": ["a.py"]}
    _write_dispatch(tmp_root, "002-01", state="failed", chat_pending=False, finished=iso(now()))
    _write_dispatch(tmp_root, "002-02", state="done", finished=iso(now()), chat_pending=True)
    _write_dispatch(tmp_root, "002-03", state="done", finished=iso(now()), git=built)
    op = ops.status_line(tmp_root)
    assert op.text == "⏸ 002-02 done  ⏸ 002-03 done"
    assert [w["id"] for w in op.data["waiting"]] == ["002-02", "002-03"]


def test_a_closed_feature_s_dispatches_wait_on_nobody_everywhere(tmp_root: Path, run):
    """Found live: the Workers view hid a feature's two dispatches after it closed, while the
    statusline, the session-start block and `dispatch list` still counted them: 5 against 7.
    Every surface now passes the open features to `is_live`."""
    from agent_cli import dispatch

    run(["feature", "new", "a", "--title", "A"])
    run(["feature", "new", "b", "--title", "B"])
    _write_dispatch(tmp_root, "001-01", feature=1, state="done", finished=iso(now()), chat_pending=True)
    _write_dispatch(tmp_root, "002-01", feature=2, state="blocked", finished=iso(now()))
    assert ops.status_line(tmp_root).text == "⏸ 001-01 done  ⏸ 002-01 blocked"
    run(["feature", "close", "2"])
    assert ops.status_line(tmp_root).text == "⏸ 001-01 done"
    assert "002-01" not in dispatch.pending_block(tmp_root)
    assert [d["id"] for d in dispatch.list_(tmp_root)] == ["001-01"]


def test_acked_abandoned_cancelled_and_probes_do_not_render(tmp_root: Path):
    _write_dispatch(
        tmp_root, "002-01", state="done", finished="2026-09-03T10:00:00-07:00", actioned_at=iso(now())
    )
    _write_dispatch(tmp_root, "002-02", state="abandoned")
    _write_dispatch(tmp_root, "002-03", state="cancelled")
    _write_dispatch(tmp_root, "002-04", state="question", kind="probe")
    assert ops.status_line(tmp_root).text == ""


def test_running_and_waiting_together(tmp_root: Path):
    _write_dispatch(tmp_root, "002-01", state="running", started=iso(now()), cost_usd=0.0)
    _write_dispatch(tmp_root, "002-02", state="question", chat_pending=True)
    op = ops.status_line(tmp_root)
    assert op.text == "⚙ 002-01 alpha 0m  ⏸ 002-02 question"


def test_a_session_sees_its_name_its_own_workers_and_its_peers(tmp_root: Path, monkeypatch):
    """A manager session's line names the session, names the workers it launched,
    counts the rest, and lists the other live manager sessions by name. another live
    manager's unread results are that manager's actions, not the owner's, so they are not
    counted here; an orphan whose launcher has ended is."""
    from agent_cli import core, sessions

    monkeypatch.setattr(core, "is_alive", lambda pid: pid in (1, 2))
    sessions.set_record(tmp_root, "me", name="mgr-020-worker-management", pid=1)
    sessions.set_record(tmp_root, "peer", name="mgr-006-example", pid=2)
    sessions.set_record(tmp_root, "gone", name="mgr-005-old", pid=3)
    sessions.set_record(tmp_root, "worker", dispatch_id="003-05", pid=2)
    _write_dispatch(tmp_root, "004-01", state="question", launched_by_session="me")
    _write_dispatch(tmp_root, "003-04", state="done", launched_by_session="peer", chat_pending=True)
    _write_dispatch(tmp_root, "003-05", state="done", launched_by_session="gone", chat_pending=True)
    _write_dispatch(tmp_root, "003-06", state="blocked", launched_by_session="peer")
    op = ops.status_line(tmp_root, session_id="me")
    assert op.text == (
        "[mgr-020-worker-management]  ⏸ 004-01 question  ⏸ 2 more waiting on you  peers: mgr-006-example"
    )
    assert op.data["peers"] == ["mgr-006-example"]


def test_lease_for_stdin_session_renders(tmp_root: Path):
    leases_mod.take(tmp_root, "vault:src", session_id="sess-a", pid=os.getpid())
    op = ops.status_line(tmp_root, session_id="sess-a")
    assert op.text == "leases: vault:src"


def test_lease_for_a_different_session_is_not_shown(tmp_root: Path):
    leases_mod.take(tmp_root, "vault:src", session_id="sess-a", pid=os.getpid())
    op = ops.status_line(tmp_root, session_id="sess-b")
    assert op.text == ""


def test_no_session_id_shows_every_live_lease_by_holder(tmp_root: Path):
    leases_mod.take(tmp_root, "vault:src", session_id="sess-a", pid=os.getpid())
    op = ops.status_line(tmp_root, session_id=None)
    assert op.text == "leases held: vault:src (sess-a)"


def test_nothing_renders_as_empty(tmp_root: Path):
    assert ops.status_line(tmp_root).text == ""


def test_cli_reads_session_id_from_stdin_json(tmp_root: Path, run):
    leases_mod.take(tmp_root, "vault:harness", session_id="sess-x", pid=os.getpid())
    payload = json.dumps({"session_id": "sess-x", "cwd": str(tmp_root)})
    r = run(["status", "line"], stdin=payload)
    assert r.code == 0
    assert r.out.strip() == "leases: vault:harness"


def test_session_cost_is_recorded_from_the_statusline_payload(tmp_root: Path, run):
    """Claude Code prices the session itself and hands the total over on stdin.
    It is the only dollar figure the harness gets for its own work, so it is stored: cumulative
    on the record, and split per local day for the journal."""
    from agent_cli import core, sessions

    day = core.today().isoformat()
    payload = json.dumps({"session_id": "sess-c", "cost": {"total_cost_usd": 1.2345}})
    r = run(["status", "line"], stdin=payload)
    assert r.code == 0
    assert r.out.strip() == ""  # no name, no feature, nothing else to say yet
    assert (sessions.get(tmp_root, "sess-c") or {})["cost_usd"] == 1.2345
    assert sessions.cost_summary_for_day(tmp_root, day)[0] == 1.2345

    sessions.set_record(tmp_root, "sess-c", name="mgr-022-live")
    payload = json.dumps({"session_id": "sess-c", "cost": {"total_cost_usd": 4.0}})
    r = run(["status", "line"], stdin=payload)
    assert r.out.strip() == "[mgr-022-live $4.00]"
    # the delta, not the total, so a session that works past midnight splits across both days
    assert sessions.cost_summary_for_day(tmp_root, day)[0] == 4.0
    assert (sessions.get(tmp_root, "sess-c") or {})["cost_by_day"] == {day: 4.0}


def test_an_unchanged_total_writes_nothing(tmp_root: Path):
    """The status line runs on every assistant message and on a 15s timer. A record rewrite per
    tick is a write Claude Code waits on."""
    from agent_cli import core, sessions

    day = core.today().isoformat()
    sessions.record_cost(tmp_root, "sess-d", 2.0, day)
    before = (tmp_root / ".agent" / "sessions" / "sess-d.json").stat().st_mtime_ns
    assert sessions.record_cost(tmp_root, "sess-d", 2.004, day) is None
    assert (tmp_root / ".agent" / "sessions" / "sess-d.json").stat().st_mtime_ns == before
    assert sessions.cost_summary_for_day(tmp_root, day)[0] == 2.0


def test_a_cost_that_restarts_adds_nothing(tmp_root: Path):
    """A resumed session can hand over a total lower than the one already stored. The day's
    figure must not go backwards, and a negative delta is not spend."""
    from agent_cli import core, sessions

    day = core.today().isoformat()
    sessions.record_cost(tmp_root, "sess-e", 5.0, day)
    assert sessions.record_cost(tmp_root, "sess-e", 1.0, day) is None
    assert sessions.cost_summary_for_day(tmp_root, day)[0] == 5.0


def test_cli_empty_stdin_is_the_no_session_view(tmp_root: Path, run):
    leases_mod.take(tmp_root, "vault:src", session_id="sess-a", pid=os.getpid())
    r = run(["status", "line"], stdin="")
    assert r.code == 0
    assert r.out.strip() == "leases held: vault:src (sess-a)"


def test_cli_malformed_stdin_does_not_crash(tmp_root: Path, run):
    r = run(["status", "line"], stdin="not json")
    assert r.code == 0
    assert r.out == ""
