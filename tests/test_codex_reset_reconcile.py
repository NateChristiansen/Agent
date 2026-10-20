"""every eval opened a new window, and each fix patched one.

A resume marked during the settle eval left the tab blank: the settle deleted the marker and
reported the reset as run, and apply counted it painted without reading the record again. Now
no eval but the last deletes the marker, and every way out of `tab_reset.apply` goes through one
reconcile step that reads the record after the last eval and acts on it."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import sessions, tab_drain, tab_reset
from test_codex_reset_race import (
    DISPLAY,
    OTHER,
    SID,
    STALE,
    FakeObsidian,
    _ended_with_a_queued_reset,
    needs_node,
)

RESTORED = f"✅ {DISPLAY}"


@needs_node
def test_a_resume_during_the_settle_is_restored_and_the_marker_cleared(tmp_root: Path, monkeypatch):
    """The reviewer's probe: the reset runs but its reply times out; while the settle is paused a
    fallback resume marks the thread and its state goes idle; then the settle runs."""
    _ended_with_a_queued_reset(tmp_root, monkeypatch)
    monkeypatch.setattr(tab_reset, "RESET_LOCK_WAIT_S", 0.05)
    fake = FakeObsidian([STALE, OTHER]).install(monkeypatch)
    fake.report[1] = "timeout"

    def resume_during_settle() -> None:
        tab_reset.mark_resumed(tmp_root, SID, sessions.get(tmp_root, SID) or {})
        sessions.set_field(tmp_root, SID, "activity", {"state": "idle", "at": "2026-09-03T14:06:00-07:00"})

    fake.before[2] = resume_during_settle
    tab_drain.run(tmp_root)
    record = sessions.get(tmp_root, SID) or {}
    assert record[tab_drain.RESUMED_FIELD] is True
    assert fake.log == [(0, STALE, ""), (0, "", RESTORED)]
    assert fake.tabs == [RESTORED, OTHER] and fake.marks == [None, None]
    assert record.get(tab_reset.RESTORE_FAILED_FIELD) is None


def _one_reset(tmp_root: Path, monkeypatch) -> tuple[FakeObsidian, dict]:
    _ended_with_a_queued_reset(tmp_root, monkeypatch)
    monkeypatch.setattr(tab_reset, "RESET_LOCK_WAIT_S", 0.05)
    (entry,) = tab_drain._pending(tmp_root, tab_drain._state(tmp_root))
    return FakeObsidian([STALE, OTHER]).install(monkeypatch), entry


def _resume(tmp_root: Path):
    def resume() -> None:
        tab_reset.mark_resumed(tmp_root, SID, sessions.get(tmp_root, SID) or {})
        sessions.set_field(tmp_root, SID, "activity", {"state": "idle", "at": "2026-09-03T14:06:00-07:00"})

    return resume


def _records(tmp_root: Path) -> tuple[object, object]:
    record = sessions.get(tmp_root, SID) or {}
    return record.get(tab_reset.RESTORE_FAILED_FIELD), record.get(tab_reset.UNSETTLED_FIELD)


@needs_node
def test_an_acknowledged_reset_while_ended_stands_and_its_marker_goes(tmp_root: Path, monkeypatch):
    fake, entry = _one_reset(tmp_root, monkeypatch)
    assert tab_reset.apply(tmp_root, entry) == {entry["key"]}
    assert fake.log == [(0, STALE, "")] and fake.marks == [None, None]
    assert len(fake.codes) == 2  # the reset, then the reconcile's unmark
    assert _records(tmp_root) == (None, None)


@needs_node
def test_an_unknown_reply_while_ended_is_settled_as_run_and_stands(tmp_root: Path, monkeypatch):
    fake, entry = _one_reset(tmp_root, monkeypatch)
    fake.report[1] = "garbled"
    assert tab_reset.apply(tmp_root, entry) == {entry["key"]}
    assert fake.log == [(0, STALE, "")] and fake.marks == [None, None]
    assert _records(tmp_root) == (None, None)


@needs_node
def test_a_confirmed_cancellation_changes_nothing_now_or_later(tmp_root: Path, monkeypatch):
    fake, entry = _one_reset(tmp_root, monkeypatch)
    fake.fail[1] = "timeout"
    assert tab_reset.apply(tmp_root, entry) == set()
    _resume(tmp_root)()
    assert "cancelled" in fake.execute(fake.codes[0])
    assert fake.log == [] and fake.marks == [None, None]
    assert _records(tmp_root) == (None, None)


@needs_node
def test_an_unconfirmed_cancellation_is_recorded(tmp_root: Path, monkeypatch):
    fake, entry = _one_reset(tmp_root, monkeypatch)
    fake.fail[1] = "timeout"
    fake.fail[2] = "exit"
    assert tab_reset.apply(tmp_root, entry) == set()
    restore_failed, unsettled = _records(tmp_root)
    assert restore_failed is None and isinstance(unsettled, dict) and unsettled["marker"] in fake.codes[0]
    assert fake.log == []


@needs_node
def test_a_resume_during_the_reset_eval_is_restored(tmp_root: Path, monkeypatch):
    fake, entry = _one_reset(tmp_root, monkeypatch)
    fake.before[1] = _resume(tmp_root)
    assert tab_reset.apply(tmp_root, entry) == set()
    assert fake.log == [(0, STALE, ""), (0, "", RESTORED)] and fake.marks == [None, None]
    assert _records(tmp_root) == (None, None)


@needs_node
def test_a_failed_reconcile_is_recorded_and_keeps_the_marker(tmp_root: Path, monkeypatch):
    fake, entry = _one_reset(tmp_root, monkeypatch)
    fake.report[1] = "timeout"
    fake.before[2] = _resume(tmp_root)
    fake.fail[3] = "timeout"
    assert tab_reset.apply(tmp_root, entry) == set()
    assert fake.log == [(0, STALE, "")]
    assert fake.marks[0] is not None and fake.marks[0] in fake.codes[0]
    restore_failed, _ = _records(tmp_root)
    assert isinstance(restore_failed, dict) and restore_failed["label"] == RESTORED


KEY = f"manager-{SID}"
# scenario: (eval replies, wanted, trouble, expected result, evals run, unsettled recorded)
SCENARIOS = {
    "not wanted": ([], False, None, set(), 0, False),
    "reset done": (["AGENT_TAB_RESET:done", "AGENT_TAB_UNMARK:done"], True, None, {KEY}, 2, False),
    "no match": (["AGENT_TAB_RESET:none"], True, None, set(), 1, False),
    "ambiguous": (["AGENT_TAB_RESET:ambiguous"], True, None, {f"ambiguous.{KEY}"}, 1, False),
    "cancelled": (["AGENT_TAB_RESET:cancelled"], True, None, set(), 1, False),
    "lost, ran": ([None, "AGENT_TAB_SETTLE:marked", "AGENT_TAB_UNMARK:done"], True, None, {KEY}, 3, False),
    "lost, not run": ([None, "AGENT_TAB_SETTLE:cancelled"], True, None, set(), 2, False),
    "lost, unsettled": ([None, None], True, None, set(), 2, True),
    "garbled": (
        ["=> undefin", "AGENT_TAB_SETTLE:marked", "AGENT_TAB_UNMARK:done"],
        True,
        None,
        {KEY},
        3,
        False,
    ),
    "eval raises": ([], True, RuntimeError("boom"), set(), 2, True),
    "lock busy": ([], True, "lock", set(), 0, False),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_way_out_of_apply_goes_through_reconcile(tmp_root: Path, monkeypatch, scenario: str):
    """A property over every path, with the eval layer stubbed: apply leaves through reconcile
    exactly once, with the result, the number of evals and the records that path calls for."""
    outputs, wanted, trouble, expected, evals, unsettled = SCENARIOS[scenario]
    calls: list[str] = []
    sessions.set_record(tmp_root, SID, name=f"mgr-codex-{SID[-8:]}")
    sessions.set_field(tmp_root, SID, "activity", {"state": "ended"})
    replies = list(outputs)

    def run_eval(code: str, timeout: float):
        calls.append(code)
        if isinstance(trouble, Exception):
            raise trouble
        return replies.pop(0) if replies else None

    monkeypatch.setattr(tab_reset, "_run_eval", run_eval)
    monkeypatch.setattr(tab_reset, "wanted", lambda root, entry: wanted)
    original = tab_reset._reconcile
    passed: list[str] = []

    def reconcile(root, attempt):
        passed.append(attempt.outcome)
        return original(root, attempt)

    monkeypatch.setattr(tab_reset, "_reconcile", reconcile)
    entry = {"key": f"manager-{SID}", "identity": SID, "match": DISPLAY, "label": "", "reset": True}
    if trouble == "lock":
        monkeypatch.setattr(tab_reset, "RESET_LOCK_WAIT_S", 0.05)
        with tab_reset.reset_lock(tmp_root, SID):
            result = tab_reset.apply(tmp_root, entry)
    else:
        result = tab_reset.apply(tmp_root, entry)
    assert len(passed) == 1, f"{scenario}: apply left without reconcile"
    assert result == expected and len(calls) == evals
    assert bool((sessions.get(tmp_root, SID) or {}).get(tab_reset.UNSETTLED_FIELD)) is unsettled
    assert not (sessions.get(tmp_root, SID) or {}).get(tab_reset.RESTORE_FAILED_FIELD)
