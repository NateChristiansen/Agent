"""A bug: after quitting Codex the tab still read "✅ new · cx b11e" minutes later,
and after `/clear` the old name and green check stayed over a new working thread (002, Codex CLI
0.160.0, macOS and Windows). `SessionEnd` maps to `ended`, which `label_for` has no glyph for, so
the end repainted the at-rest check. The owner chose the plain terminal title instead."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli import obsidian_tabs, sessions, tab_drain, tab_reset
from agent_cli.hooks.activity import codex_activity
from hooks_test_helpers import enable_tab_drain as _enable

activity_mod = importlib.import_module("agent_cli.hooks.activity")

SID = "019b76da-a800-7000-8000-00000000b11e"
DISPLAY = "new · cx 0000"


def _claimed(tmp_root: Path) -> None:
    sessions.set_record(tmp_root, SID, name=f"mgr-codex-{SID[-8:]}")
    sessions.set_field(tmp_root, SID, "provider", "codex")
    sessions.set_field(tmp_root, SID, "tab_display_name", DISPLAY)
    sessions.set_field(tmp_root, SID, "tab_label", f"✅ {DISPLAY}")
    sessions.set_field(tmp_root, SID, "activity", {"state": "idle", "at": "2026-09-03T14:04:00-07:00"})


def _asked(monkeypatch) -> list[dict]:
    asked: list[dict] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, sid, match, label, **kw: (
            asked.append({"match": match, "label": label, **kw}) or {"started": True}
        ),
    )
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: True)
    monkeypatch.setattr("agent_cli.codex_tab_link.marker_for", lambda s: "")
    return asked


def test_a_codex_end_resets_its_one_tab_to_the_plain_title(tmp_root: Path, monkeypatch):
    _claimed(tmp_root)
    asked = _asked(monkeypatch)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})
    assert asked == [{"match": DISPLAY, "label": "", "generated": False, "reset": True}]
    # the display stays on the record, so `codex resume` can still find the tab
    assert (sessions.get(tmp_root, SID) or {})["tab_display_name"] == DISPLAY


def _resumed(tmp_root: Path, monkeypatch) -> list[dict]:
    """A thread resumed in a ribbon tab whose marker proves the claim, so it has its display back."""
    _claimed(tmp_root)
    asked = _asked(monkeypatch)
    monkeypatch.setattr(
        "agent_cli.codex_tab_link.marker_for", lambda s: "codex:" + "4" * 8 + "-4444-4444-8444-" + "4" * 12
    )
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionStart", "source": "resume"})
    assert (sessions.get(tmp_root, SID) or {})["tab_display_name"] == DISPLAY
    asked.clear()
    return asked


def test_an_old_end_long_after_a_resume_does_not_clear_the_resumed_label(tmp_root: Path, monkeypatch):
    """`codex resume` keeps the thread id, so the resumed tab carries the same display, and Codex
    sends an end about a minute late. Nothing in the payload or the record says which process
    sent an end: the hooks run in the shared daemon, and a pid lookup by thread id finds the
    resumed process whichever one ended. So no time window decides it (008 review, 2026-10-03)."""
    clock = [1000.0]
    monkeypatch.setattr(activity_mod.time, "time", lambda: clock[0])
    asked = _resumed(tmp_root, monkeypatch)
    clock[0] = 1000.0 + 600  # far outside any window a late end was ever seen in
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})
    assert not any(ask.get("reset") for ask in asked)
    # a worker finishing later repaints the manager; the end still clears nothing
    activity_mod.refresh_tab(tmp_root, SID)
    assert not any(ask.get("reset") for ask in asked)


def test_a_real_quit_right_after_a_resume_keeps_its_label_and_says_why(tmp_root: Path, monkeypatch):
    """The documented limit: a real quit after a resume cannot be told from the old process's
    late end, so the tab keeps its last label, the green check of bug 8, and the record says
    the reset was skipped rather than leaving it silent."""
    asked = _resumed(tmp_root, monkeypatch)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})
    assert asked == []
    record = sessions.get(tmp_root, SID) or {}
    assert record["activity"]["state"] == "ended"
    assert record["tab_display_name"] == DISPLAY
    assert record[activity_mod.RESET_SKIPPED_FIELD] == activity_mod.AMBIGUOUS_END


def test_an_end_with_no_claimed_tab_asks_for_nothing(tmp_root: Path, monkeypatch):
    sessions.set_record(tmp_root, SID, name=f"mgr-codex-{SID[-8:]}")
    sessions.set_field(tmp_root, SID, "provider", "codex")
    asked = _asked(monkeypatch)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})
    assert asked == []


def test_the_drainer_keeps_a_reset_for_a_dead_pid_and_drops_it_once_resumed(tmp_root: Path, monkeypatch):
    """Other requests for a record whose known pid is dead are dropped, which left an ended
    session with a proven pid out of the reset. A reset is for a gone session, so it
    stays; it is dropped once the session is no longer ended, because a resume owns the tab."""
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain.core, "is_alive", lambda pid: False)
    sessions.set_record(tmp_root, SID, pid=987654, name=f"mgr-codex-{SID[-8:]}")
    sessions.set_field(tmp_root, SID, "activity", {"state": "ended"})
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, "", generated=False, reset=True)
    pending = tab_drain._pending(tmp_root, tab_drain._state(tmp_root))
    assert [(e["match"], e["label"], e["token"]) for e in pending] == [(DISPLAY, "", "")]

    sessions.set_field(tmp_root, SID, "activity", {"state": "idle"})
    assert tab_drain._pending(tmp_root, tab_drain._state(tmp_root)) == []


def _stub_reset_paint(monkeypatch, painted: list[dict], out: str = "AGENT_TAB_RESET:done") -> None:
    """A reset paints through its own eval (`tab_reset`); record each one instead."""
    monkeypatch.setattr(
        tab_reset, "_run_eval", lambda code, timeout: painted.append({"reset": True, "code": code}) or out
    )


def test_a_reset_queued_before_a_resume_never_lands_after_a_late_end(tmp_root: Path, monkeypatch):
    """the first end queued a reset, a resume with no marker sent no new
    request, so the old reset stayed queued; a late end then made the record ended again, and the
    drainer would have cleared the resumed thread's tab with it."""
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: False)
    monkeypatch.setattr("agent_cli.codex_tab_link.marker_for", lambda s: "")
    _claimed(tmp_root)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})
    queued = tab_drain.read_entry(tmp_root / tab_drain.TABS_DIR / f"manager-{SID}.json")
    assert queued["reset"] is True and queued["match"] == DISPLAY

    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionStart", "source": "resume"})
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})  # the late end
    assert (sessions.get(tmp_root, SID) or {})["activity"]["state"] == "ended"

    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    applied: list[dict] = []
    monkeypatch.setattr(
        tab_drain, "_eval", lambda entries, **kw: applied.extend(entries) or {e["key"] for e in entries}
    )
    _stub_reset_paint(monkeypatch, applied)
    tab_drain.run(tmp_root)
    assert not any(entry.get("reset") for entry in applied)


def test_a_resume_between_selecting_and_applying_a_reset_stops_it(tmp_root: Path, monkeypatch):
    """the drainer selects its entries under its lock, releases
    it, then paints. A resume in that gap set `resumed` and withdrew the request, and the cached
    reset was painted anyway. Forced order: select, resume, then apply."""
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: False)
    monkeypatch.setattr("agent_cli.codex_tab_link.marker_for", lambda s: "")
    _claimed(tmp_root)
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)

    painted: list[dict] = []
    monkeypatch.setattr(
        tab_drain, "_eval", lambda entries, **kw: painted.extend(entries) or {e["key"] for e in entries}
    )
    _stub_reset_paint(monkeypatch, painted)
    original_apply = tab_drain._apply
    resumed: list[bool] = []

    def resume_then_apply(root, entries):
        if not resumed:
            # the reset was selected; after the resume the drainer paints the snapshot's label
            assert any(entry.get("reset") for entry in entries)
            codex_activity(
                tmp_root, {"session_id": SID, "hook_event_name": "SessionStart", "source": "resume"}
            )
            resumed.append(True)
        return original_apply(root, entries)

    monkeypatch.setattr(tab_drain, "_apply", resume_then_apply)
    tab_drain.run(tmp_root)
    assert resumed and not any(entry.get("reset") for entry in painted)


def test_a_resume_waits_on_a_held_reset_lock_only_for_its_bound(tmp_root: Path, monkeypatch):
    """The resume runs in a hook with a 5 s timeout, so a held lock costs it a bounded wait and
    the thread is still marked resumed."""
    import time

    _claimed(tmp_root)
    monkeypatch.setattr(tab_reset, "RESET_LOCK_WAIT_S", 0.1)
    with tab_reset.reset_lock(tmp_root, SID):
        started = time.monotonic()
        activity_mod.mark_resumed(tmp_root, SID, sessions.get(tmp_root, SID) or {})
        assert time.monotonic() - started < 1.0
    assert (sessions.get(tmp_root, SID) or {})[tab_drain.RESUMED_FIELD] is True


def test_the_drainer_and_the_queue_each_refuse_a_reset_for_a_resumed_thread(tmp_root: Path, monkeypatch):
    """Either guard holds alone: a reset that outlives the resume's cancel, say by a race, is
    still dropped by the drainer, and no new one is queued."""
    _enable(monkeypatch)
    _claimed(tmp_root)
    sessions.set_field(tmp_root, SID, "activity", {"state": "ended"})
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, "", generated=False, reset=True)
    sessions.set_field(tmp_root, SID, tab_drain.RESUMED_FIELD, True)
    assert tab_drain._pending(tmp_root, tab_drain._state(tmp_root)) == []
    assert tab_drain.request(tmp_root, "manager", SID, DISPLAY, "", generated=False, reset=True) == {
        "started": False,
        "reason": "resumed",
    }


def test_a_found_reset_keeps_the_display_and_zero_matches_stop_after_the_retry_cap(
    tmp_root: Path, monkeypatch
):
    _enable(monkeypatch)
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    _claimed(tmp_root)
    sessions.set_field(tmp_root, SID, "activity", {"state": "ended"})
    tab_drain.request(tmp_root, "manager", SID, DISPLAY, "", generated=False, reset=True)
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)
    calls: list[dict] = []
    _stub_reset_paint(monkeypatch, calls, "AGENT_TAB_RESET:none")
    tab_drain.run(tmp_root)
    assert len(calls) == tab_drain.RETRY_LIMIT  # no tab carries it: left alone

    tab_drain._save_state(tmp_root, {"runner": os.getpid()})
    _stub_reset_paint(monkeypatch, calls)
    tab_drain.run(tmp_root)
    assert tab_drain._state(tmp_root)["painted"][f"manager-{SID}"]["label"] == ""
    record = sessions.get(tmp_root, SID) or {}
    assert record["tab_display_name"] == DISPLAY


def _run_js(code: str, titles: list[str]) -> list:
    setup = (
        "const leaves = "
        + json.dumps([{"id": f"leaf{i}", "view": {"state": {"userTitle": t}}} for i, t in enumerate(titles)])
        + "; for (const l of leaves) l.getDisplayText = function () { return this.view.state.userTitle; };"
        " globalThis.app = {workspace: {getLeavesOfType: () => leaves}};"
    )
    script = (
        setup
        + f"console.log(JSON.stringify([eval({json.dumps(code)}), leaves.map(l => l.view.state.userTitle)]));"
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to execute Obsidian eval JS")
def test_a_reset_clears_one_matching_tab_and_refuses_two():
    code = tab_reset.reset_code(DISPLAY, "m1")
    other = "✅ 001 example · cx 0001"
    assert _run_js(code, [other, f"✅ {DISPLAY}"]) == ["AGENT_TAB_RESET:done", [other, ""]]
    assert _run_js(code, [other]) == ["AGENT_TAB_RESET:none", [other]]
    assert _run_js(code, [f"✅ {DISPLAY}", f"⚙️ {DISPLAY}"]) == [
        "AGENT_TAB_RESET:ambiguous",
        [f"✅ {DISPLAY}", f"⚙️ {DISPLAY}"],
    ]
