"""A dispatch's terminal commit saves its ledger sources with `finished`, publication appends them
once and clears them, and the ledger's heal finishes whatever a crash left. Records committed
without the intent are history and never become owed."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, ops, reconcile
from agent_cli import features as features_mod
from agent_cli import log as log_mod
from agent_cli.dispatch import state, terminal, verdicts
from agent_cli.ops.reconcile import reconcile as reconcile_op
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- droot is a pytest fixture

FINDING = {"severity": "blocking", "summary": "the retry loop never ends"}


@pytest.fixture(autouse=True)
def manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """These run as a manager would: a worker's inherited dispatch id would hide its failures."""
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)


def _done(root: Path, fake_claude: Any, **kw: Any) -> dict[str, Any]:
    fake_claude.scenario([one(envelope("done", findings=[FINDING]))])
    return _new_fast(root, tasks=[1], **kw)


def _feature(root: Path) -> features_mod.Feature:
    return features_mod.resolve(root, "1")


def _recs(root: Path) -> list[reconcile.Rec]:
    parsed = [reconcile.parse_entry(e) for e in log_mod.parse(_feature(root).log_path)]
    return [r for r in parsed if isinstance(r, reconcile.Rec) and r.verb == "source"]


def _sources(root: Path) -> list[str]:
    return [r.data["source"] for r in _recs(root)]


def _owed(root: Path) -> list[tuple[str, str]]:
    return sorted((i.source, i.item) for i in reconcile.ledger(root, "1").owed)


def _expected(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (f"accept:{d['id']}", "task#1"),
        (f"dispatch:{d['id']}", "F1"),
        (f"dispatch:{d['id']}", "result"),
    ]


def test_publication_registers_the_result_and_no_diff_acceptance_once(droot: Path, fake_claude):
    d = _done(droot, fake_claude)
    after = state.load(droot, d["id"])
    assert after["state"] == "done" and reconcile.INTENT not in after
    assert _sources(droot) == [f"dispatch:{d['id']}", f"accept:{d['id']}"]
    # the feature's first new event: it finished before its entry was written and is owed anyway
    assert all(
        datetime.fromisoformat(after["finished"]) <= datetime.fromisoformat(r.at) for r in _recs(droot)
    )
    assert _owed(droot) == _expected(d)
    reconcile.ledger(droot, "1")
    assert len(_sources(droot)) == 2


def test_the_intent_is_saved_in_the_commit_before_any_append(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    seen: list[Any] = []

    def crash(feature, pending):
        record = state.load(droot, pending["source"].split(":", 1)[1])
        seen.append((record.get("finished"), [p["source"] for p in record.get(reconcile.INTENT, [])]))
        raise OSError("killed before the append")

    with monkeypatch.context() as patched:
        patched.setattr(reconcile, "append_source", crash)
        d = _done(droot, fake_claude)
    finished, listed = seen[0]
    assert finished and listed == [f"dispatch:{d['id']}", f"accept:{d['id']}"]
    assert [p["source"] for p in state.load(droot, d["id"])[reconcile.INTENT]] == listed
    assert _sources(droot) == []
    assert _owed(droot) == _expected(d)  # the heal appends once and clears
    assert reconcile.INTENT not in state.load(droot, d["id"])
    reconcile.ledger(droot, "1")
    assert len(_sources(droot)) == 2


def test_a_crash_after_the_append_before_the_clear_heals_without_a_second_entry(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    real = terminal.terminal_guard

    def guard(*a, **k):
        if sys._getframe(1).f_code.co_name == "_register":
            raise OSError("killed before the clear")
        return real(*a, **k)

    with monkeypatch.context() as patched:
        patched.setattr(terminal, "terminal_guard", guard)
        d = _done(droot, fake_claude)
    assert len(state.load(droot, d["id"])[reconcile.INTENT]) == 2
    assert len(_sources(droot)) == 2
    assert _owed(droot) == _expected(d)
    assert reconcile.INTENT not in state.load(droot, d["id"])
    assert len(_sources(droot)) == 2


def test_concurrent_heals_leave_one_effective_source(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    def crash(*_a):
        raise OSError("killed before the append")

    with monkeypatch.context() as patched:
        patched.setattr(reconcile, "append_source", crash)
        d = _done(droot, fake_claude)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def heal() -> None:
        barrier.wait()
        try:
            reconcile.ledger(droot, "1")
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    threads = [threading.Thread(target=heal) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert 2 <= len(_sources(droot)) <= 4  # a race may append twice; the fold collapses (source, G)
    assert _owed(droot) == _expected(d)
    assert reconcile.INTENT not in state.load(droot, d["id"])


def test_old_records_without_an_intent_are_never_backfilled(droot: Path, fake_claude):
    # what an older build committed: a finished result with findings and an open task, no intent
    old = state.save(
        droot,
        {
            "id": "001-old-01",
            "repo": "alpha",
            "short": "old",
            "feature": 1,
            "kind": "work",
            "attempt": 1,
            "state": "done",
            "tasks": [1],
            "finished": "2020-01-01T00:00:00+00:00",
            "result": {"status": "done", "summary": "s", "findings": [FINDING]},
        },
    )
    assert reconcile.ledger(droot, "1").owed == []
    assert _sources(droot) == []
    new = _done(droot, fake_claude)
    owed = _owed(droot)
    assert owed == _expected(new)
    assert not any(source.endswith(old["id"]) for source, _ in owed)


def test_reading_consuming_and_acknowledging_settle_nothing(droot: Path, fake_claude):
    d = _done(droot, fake_claude, session_id="mgr-1")
    ops.dispatch_show(droot, dispatch_id=d["id"])
    ops.dispatch_consume(droot, dispatch_id=d["id"], session_id="mgr-1")
    ops.dispatch_ack(droot, dispatch_id=d["id"], note="read it")
    assert verdicts.is_read(state.load(droot, d["id"]))
    assert _owed(droot) == _expected(d)


def test_task_done_settles_no_diff_acceptance_and_leaves_the_result_owed(droot: Path, fake_claude):
    d = _done(droot, fake_claude)
    ops.task_done(droot, task="1", feature="1")
    assert _owed(droot) == _expected(d)[1:]
    led = reconcile.ledger(droot, "1")
    assert led.item(f"accept:{d['id']}", "task#1").state == "settled"  # type: ignore[union-attr]


def test_a_batched_decision_settles_every_item_of_the_result(droot: Path, fake_claude):
    d = _done(droot, fake_claude)
    rows = [
        {
            "source": f"dispatch:{d['id']}",
            "item": item,
            "disposition": "no-task",
            "reason": "read; nothing to do",
        }
        for item in ("result", "F1")
    ]
    reconcile_op(droot, feature="1", items=rows)
    assert _owed(droot) == _expected(d)[:1]


def test_a_question_raises_nothing(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("question", open_questions=["which port?"]))])
    d = _new_fast(droot, tasks=[1])
    assert state.load(droot, d["id"])["state"] == "question"
    assert reconcile.INTENT not in state.load(droot, d["id"])
    assert _sources(droot) == []


def test_a_done_probe_naming_an_open_task_raises_nothing(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("done"))])
    d = _new_fast(droot, tasks=[1], kind="probe")
    assert state.load(droot, d["id"])["state"] == "done"
    assert reconcile.INTENT not in state.load(droot, d["id"])
    assert _sources(droot) == [] and _owed(droot) == []


def test_a_late_collect_of_a_never_published_attempt_registers_it(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch
):
    fake_claude.scenario([one(envelope("blocked", findings=[FINDING]))])
    with monkeypatch.context() as patched:
        patched.setattr(terminal, "requires_lease", lambda *_args: True)
        d = _new_fast(droot, tasks=[1])
    assert not state.load(droot, d["id"])["terminal_committed"] and _sources(droot) == []
    dispatch.collect(droot, dispatch_id=d["id"])
    assert state.load(droot, d["id"])["terminal_committed"]
    assert _owed(droot) == _expected(d)[1:]
