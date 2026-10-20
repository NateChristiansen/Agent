"""Compact dispatch results and a worker's last sign of life (phase F)."""

from __future__ import annotations

import inspect
import os
import time
from pathlib import Path
from typing import Any

from agent_cli.mcp import specs_dispatch
from agent_cli.mcp.tools import Op
from agent_cli.ops.dispatch_summary import SILENT_MINUTES, activity, compact
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- pytest fixture

RECORD: dict[str, Any] = {
    "id": "001-mac-12",
    "state": "done",
    "attempt": 4,
    "provider": "claude",
    "cost_usd": 12.63,
    "budget_usd": 15.0,
    "thread": [{"text": "x" * 20_000}] * 20,
    "options": {"env": ["A"] * 500},
    "last_result": {"structured_output": {"status": "done", "summary": "report built"}},
    "verification_result": {"status": "verified", "reasons": []},
}


def test_compact_keeps_what_a_manager_acts_on() -> None:
    out = compact(RECORD)
    assert out["id"] == "001-mac-12" and out["state"] == "done" and out["attempt"] == 4
    assert out["verification"] == "verified"
    assert "thread" not in out and "options" not in out
    assert len(str(out)) < 1_000 < len(str(RECORD))


def test_changing_actions_return_compact_records_unless_verbose() -> None:
    wrapped = specs_dispatch._compact_results(lambda ctx, action, **kw: Op(dict(RECORD), "acknowledged"))  # pyright: ignore[reportPrivateUsage]
    for action in ("ack", "new", "resume", "cancel", "abandon", "say"):
        assert "thread" not in wrapped(None, action=action).data
    assert "thread" in wrapped(None, action="ack", verbose=True).data
    assert "thread" in wrapped(None, action="new", dry_run=True).data
    assert "thread" in wrapped(None, action="show").data


def test_the_tool_schema_still_lists_every_parameter() -> None:
    params = inspect.signature(specs_dispatch.dispatch).parameters
    assert {"action", "dispatch_id", "writes", "verbose", "dry_run"} <= set(params)


def test_a_silent_running_worker_is_named_not_stopped(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    d["state"] = "running"
    out = droot / ".agent" / "dispatches" / f"{d['id']}.out"
    err = droot / ".agent" / "dispatches" / f"{d['id']}.err"
    old = time.time() - (SILENT_MINUTES + 30) * 60
    for path in (out, err):
        path.write_text("x", encoding="utf-8")
        os.utime(path, (old, old))
    line = activity(droot, d)
    assert "silent for" in line and "not stopped" in line
    os.utime(out, None)
    assert "silent" not in activity(droot, d)
    d["state"] = "question"
    assert "waiting on: an answer" in activity(droot, d)


def test_a_live_question_hold_waits_on_an_answer(droot: Path, fake_claude: Any) -> None:
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    d.update(state="running", open_question="1-q1")
    assert "waiting on: an answer" in activity(droot, d)


def test_watch_is_the_bounded_liveness_view(droot: Path, fake_claude: Any) -> None:
    from agent_cli import ops

    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    op = ops.dispatch_watch(droot, dispatch_id=d["id"])
    assert op.data["status"]["id"] == d["id"] and "thread" not in op.data["status"]
    assert "activity" in op.data and len(op.data["lines"]) <= 40
    assert op.text.splitlines()[0].startswith(d["id"])
