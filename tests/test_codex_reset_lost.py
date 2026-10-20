"""a reset whose response is lost.

A timeout, a nonzero exit or a garbled reply does not prove Obsidian skipped the title change.
With a resume between, the tab could be left blank with nothing to find it by, no restore and
nothing recorded. The reset now tags the leaf it clears with its own marker before clearing, so
the restore finds that leaf without the response; and a restore that finds no marker cancels the
reset in the page, so one that runs late does nothing."""

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
    _resume_that_cannot_take_the_lock,
    needs_node,
)

RESTORED = f"✅ {DISPLAY}"  # what the resumed thread's current state calls for


def _lost(tmp_root: Path, monkeypatch, tabs: list[str], marks=None) -> FakeObsidian:
    _ended_with_a_queued_reset(tmp_root, monkeypatch)
    fake = FakeObsidian(tabs, marks).install(monkeypatch)
    fake.before[1] = _resume_that_cannot_take_the_lock(tmp_root, monkeypatch)
    return fake


def _failure(tmp_root: Path):
    return (sessions.get(tmp_root, SID) or {}).get(tab_reset.RESTORE_FAILED_FIELD)


@needs_node
@pytest.mark.parametrize("lost", ["timeout", "exit", "garbled"])
def test_a_reset_that_ran_but_lost_its_response_is_restored_by_its_marker(
    tmp_root: Path, monkeypatch, lost: str
):
    fake = _lost(tmp_root, monkeypatch, [STALE, OTHER])
    fake.report[1] = lost
    tab_drain.run(tmp_root)
    assert fake.log == [(0, STALE, ""), (0, "", RESTORED)]
    assert fake.marks == [None, None]  # cleared after the restore
    assert _failure(tmp_root) is None


@needs_node
def test_an_unresolved_restore_after_a_lost_response_is_recorded(tmp_root: Path, monkeypatch):
    """Eval 1 resets and loses its reply, eval 2 settles it as marked, eval 3 (the restore) fails.
    The failure is recorded, and the marker stays on the blank tab: only a reconcile that acted
    removes it."""
    fake = _lost(tmp_root, monkeypatch, [STALE, OTHER])
    fake.report[1] = "timeout"
    fake.fail[3] = "exit"
    tab_drain.run(tmp_root)
    assert fake.log == [(0, STALE, "")]
    failure = _failure(tmp_root)
    assert failure and failure["label"] == RESTORED and failure["reason"]
    assert fake.marks[0] is not None and fake.marks[0] in fake.codes[0]


@needs_node
def test_a_reset_that_never_ran_is_cancelled_and_does_nothing_late(tmp_root: Path, monkeypatch):
    """No marker anywhere means the reset cleared nothing: it sets the marker and clears the title
    in one synchronous step. The restore cancels it in the page, so the same script arriving late
    leaves the tab alone. Nothing was cleared, so nothing is recorded and nothing restored.

    The one change is the resumed thread's tab taking the snapshot's current label, which the
    drainer paints as the plugin would: not a restore, and not a reset."""
    fake = _lost(tmp_root, monkeypatch, [STALE, OTHER])
    fake.fail[1] = "timeout"
    tab_drain.run(tmp_root)
    assert fake.log == [(0, STALE, RESTORED)] and _failure(tmp_root) is None
    late = fake.execute(fake.codes[0])
    assert "cancelled" in late and fake.tabs == [RESTORED, OTHER]


@needs_node
def test_the_restore_matches_only_its_own_reset_marker(tmp_root: Path, monkeypatch):
    """A blank tab left by another reset carries that reset's marker; it is not this one's."""
    fake = _lost(tmp_root, monkeypatch, [STALE, ""], marks=[None, "another-reset"])
    fake.report[1] = "timeout"
    tab_drain.run(tmp_root)
    assert fake.log == [(0, STALE, ""), (0, "", RESTORED)]
    assert fake.tabs[1] == "" and fake.marks == [None, "another-reset"]


def _apply_while_ended(tmp_root: Path, monkeypatch, tabs: list[str]) -> tuple[FakeObsidian, set[str]]:
    """Fifth re-review: an unknown outcome while the record is still ended used to return with
    the reset still live in the page. Apply it directly, then resume after it returns."""
    _ended_with_a_queued_reset(tmp_root, monkeypatch)
    fake = FakeObsidian(tabs).install(monkeypatch)
    return fake, set()


def _apply_then_resume(tmp_root: Path, fake: FakeObsidian) -> set[str]:
    (entry,) = tab_drain._pending(tmp_root, tab_drain._state(tmp_root))
    found = tab_reset.apply(tmp_root, entry)
    tab_reset.mark_resumed(tmp_root, SID, sessions.get(tmp_root, SID) or {})
    assert (sessions.get(tmp_root, SID) or {})[tab_drain.RESUMED_FIELD] is True
    return found


@needs_node
def test_a_timed_out_reset_is_cancelled_before_a_later_resume(tmp_root: Path, monkeypatch):
    """Exactly the probe: the reset times out before running, apply returns, the thread resumes,
    and then the timed-out script runs. It must not clear the resumed tab."""
    fake, _ = _apply_while_ended(tmp_root, monkeypatch, [STALE, OTHER])
    fake.fail[1] = "timeout"
    assert _apply_then_resume(tmp_root, fake) == set()
    late = fake.execute(fake.codes[0])
    assert "cancelled" in late and fake.tabs == [STALE, OTHER] and fake.log == []
    assert (sessions.get(tmp_root, SID) or {}).get(tab_reset.UNSETTLED_FIELD) is None


@needs_node
def test_a_cancellation_that_cannot_be_confirmed_is_recorded(tmp_root: Path, monkeypatch):
    fake, _ = _apply_while_ended(tmp_root, monkeypatch, [STALE, OTHER])
    fake.fail[1] = "timeout"
    fake.fail[2] = "exit"
    _apply_then_resume(tmp_root, fake)
    unsettled = (sessions.get(tmp_root, SID) or {})[tab_reset.UNSETTLED_FIELD]
    assert unsettled["marker"] in fake.codes[0] and unsettled["reason"]


@needs_node
def test_a_reset_that_ran_while_ended_but_lost_its_response_counts_as_painted(tmp_root: Path, monkeypatch):
    """The normal ended-tab reset still lands: its marker shows it ran, so it is settled as done."""
    fake, _ = _apply_while_ended(tmp_root, monkeypatch, [STALE, OTHER])
    fake.report[1] = "timeout"
    (entry,) = tab_drain._pending(tmp_root, tab_drain._state(tmp_root))
    assert tab_reset.apply(tmp_root, entry) == {entry["key"]}
    assert fake.log == [(0, STALE, "")] and fake.marks == [None, None]
    assert (sessions.get(tmp_root, SID) or {}).get(tab_reset.UNSETTLED_FIELD) is None


@needs_node
def test_a_tab_repainted_before_the_restore_is_left_alone(tmp_root: Path, monkeypatch):
    fake = _lost(tmp_root, monkeypatch, [STALE, OTHER])
    fake.report[1] = "timeout"
    repainted = f"⚙️ {DISPLAY}"

    def repaint() -> None:
        fake.tabs[0] = repainted
        fake.log.append((0, "", repainted))

    fake.before[2] = repaint
    tab_drain.run(tmp_root)
    # the restore leaves the repainted tab alone; then the drainer brings it to the snapshot's
    # current label, as the plugin would
    assert fake.log == [(0, STALE, ""), (0, "", repainted), (0, repainted, RESTORED)]
    assert fake.tabs[0] == RESTORED and fake.marks == [None, None]
    assert _failure(tmp_root) is None
