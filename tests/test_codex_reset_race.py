"""two orders in which a resume and a tab reset still crossed.

A resume that cannot take the reset lock in time marks the thread resumed without it, so a reset
paint already past its check could clear the resumed tab. The owner accepts that the label goes
blank briefly there, if it is restored immediately after from the session's current state. And
the resume's withdrawal of the queued reset waited on the drainer lock with no deadline, past
the hook's 5 s timeout. These tests force both orders against a stand-in Obsidian that runs the
real eval code in node and logs every title change in order."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agent_cli import obsidian_tabs, sessions, tab_drain, tab_reset
from agent_cli.hooks.activity import codex_activity

activity_mod = importlib.import_module("agent_cli.hooks.activity")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node runs the Obsidian eval JS")

SID = "019b76da-a800-7000-8000-00000000b11e"
DISPLAY = "new · cx 0000"
OTHER = "✅ 001 example · cx 0001"
STALE = f"✋ {DISPLAY}"  # what the tab read before the quit; not what the resumed thread is doing
REAL_RUN = subprocess.run


def _ended_with_a_queued_reset(tmp_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)

    class Process:
        pid = 99999

    # Not `subprocess.Popen`: the stand-in Obsidian runs node through it.
    monkeypatch.setattr(tab_drain, "_spawn", lambda argv, root: Process())
    monkeypatch.setattr(tab_drain, "INTERVAL_S", 0)
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: False)
    monkeypatch.setattr("agent_cli.codex_tab_link.marker_for", lambda s: "")  # the resume claims nothing
    sessions.set_record(tmp_root, SID, name=f"mgr-codex-{SID[-8:]}")
    sessions.set_field(tmp_root, SID, "provider", "codex")
    sessions.set_field(tmp_root, SID, "tab_display_name", DISPLAY)
    sessions.set_field(tmp_root, SID, "tab_label", STALE)
    sessions.set_field(
        tmp_root, SID, "activity", {"state": "waiting_on_you", "at": "2026-09-03T14:04:00-07:00"}
    )
    codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionEnd"})
    assert tab_drain.read_entry(tmp_root / tab_drain.TABS_DIR / f"manager-{SID}.json")["reset"] is True
    state = tab_drain._state(tmp_root)
    state["runner"] = os.getpid()
    tab_drain._save_state(tmp_root, state)


class FakeObsidian:
    """Obsidian for `obsidian eval`: runs each script in node against `tabs`, the userTitle of
    each leaf, keeping what a live window keeps between evals (properties set on a leaf, and the
    page's globals). `log` holds every title change `(leaf, before, after)` in order.

    Per eval number: `before[n]` runs while eval n is paused, under whatever lock its caller
    holds; `fail[n]` ("exit" or "timeout") fails it without running; `report[n]` runs it, then
    loses the response ("timeout", "exit" or "garbled")."""

    def __init__(self, tabs: list[str], marks: list[str | None] | None = None):
        self.tabs = tabs
        self.marks: list[str | None] = marks or [None] * len(tabs)
        self.cancelled: dict[str, bool] = {}
        self.log: list[tuple[int, str, str]] = []
        self.codes: list[str] = []
        self.before: dict[int, object] = {}
        self.fail: dict[int, str] = {}
        self.report: dict[int, str] = {}

    def execute(self, code: str) -> str:
        leaves = [{"id": f"leaf{i}", "view": {"state": {"userTitle": t}}} for i, t in enumerate(self.tabs)]
        setup = (
            f"const leaves = {json.dumps(leaves)}; const marks = {json.dumps(self.marks)};"
            " leaves.forEach((l, i) => { if (marks[i] !== null) l.agentResetToken = marks[i];"
            " l.getDisplayText = function () { return this.view.state.userTitle; }; });"
            f" globalThis.__agentResetCancelled = {json.dumps(self.cancelled)};"
            " globalThis.app = {workspace: {getLeavesOfType: () => leaves,"
            " getLeafById: (id) => leaves.find((l) => l.id === id)}};"
        )
        script = (
            setup
            + f"const out = eval({json.dumps(code)});"
            + "console.log(JSON.stringify([out, leaves.map((l) => l.view.state.userTitle),"
            " leaves.map((l) => l.agentResetToken === undefined ? null : l.agentResetToken),"
            " globalThis.__agentResetCancelled || {}]));"
        )
        done = REAL_RUN(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
        out, titles, self.marks, self.cancelled = json.loads(done.stdout)
        for i, (before, after) in enumerate(zip(self.tabs, titles, strict=True)):
            if before != after:
                self.log.append((i, before, after))
        self.tabs[:] = titles
        return str(out)

    def install(self, monkeypatch) -> FakeObsidian:
        monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")

        def run(argv, **kw):
            if argv[0] != "/usr/local/bin/obsidian":
                return REAL_RUN(argv, **kw)
            code = argv[-1].removeprefix("code=")
            self.codes.append(code)
            n = len(self.codes)
            hook = self.before.get(n)
            if callable(hook):
                hook()
            failure = self.fail.get(n)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kw.get("timeout") or 0)
            if failure == "exit":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
            out = self.execute(code)
            lost = self.report.get(n)
            if lost == "timeout":
                raise subprocess.TimeoutExpired(argv, kw.get("timeout") or 0)
            if lost == "exit":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
            if lost == "garbled":
                return subprocess.CompletedProcess(argv, 0, stdout="=> undefin", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        monkeypatch.setattr(tab_drain.subprocess, "run", run)
        return self


def _fake_obsidian(monkeypatch, tabs: list[str], before_first_eval=None, fail: dict[int, str] | None = None):
    fake = FakeObsidian(tabs)
    if before_first_eval is not None:
        fake.before[1] = before_first_eval
    fake.fail = dict(fail or {})
    return fake.install(monkeypatch).log


def _resume_that_cannot_take_the_lock(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(tab_reset, "RESET_LOCK_WAIT_S", 0.05)

    def resume() -> None:
        codex_activity(tmp_root, {"session_id": SID, "hook_event_name": "SessionStart", "source": "resume"})
        assert (sessions.get(tmp_root, SID) or {})[tab_drain.RESUMED_FIELD] is True

    return resume


@needs_node
def test_a_resume_that_times_out_during_a_reset_paint_is_restored_from_its_current_state(
    tmp_root: Path, monkeypatch
):
    """Forced order: the reset paint is paused with the reset lock held, the resume cannot take
    the lock in time and marks the thread resumed without it, then the paint completes. The tab
    goes blank, then gets the label the resumed thread's state calls for now: idle, so ✅, and not
    the stale ✋ it had before the quit.

    The resume claimed no tab (no marker, bug 4 deferred), so the record keeps no tab fields: the
    restore puts the label back on the one tab that carried this thread's exact display, and the
    thread's later events repaint it by its suffix."""
    _ended_with_a_queued_reset(tmp_root, monkeypatch)
    tabs = [STALE, OTHER]
    log = _fake_obsidian(
        monkeypatch, tabs, before_first_eval=_resume_that_cannot_take_the_lock(tmp_root, monkeypatch)
    )
    tab_drain.run(tmp_root)
    assert log == [(0, STALE, ""), (0, "", f"✅ {DISPLAY}")]
    record = sessions.get(tmp_root, SID) or {}
    assert not record.get(tab_reset.RESTORE_FAILED_FIELD)
    assert not record.get("tab_display_name")  # restored, not claimed


@needs_node
@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_a_restore_that_fails_is_recorded_and_raises_nothing(tmp_root: Path, monkeypatch, failure: str):
    _ended_with_a_queued_reset(tmp_root, monkeypatch)
    tabs = [STALE, OTHER]
    log = _fake_obsidian(
        monkeypatch,
        tabs,
        before_first_eval=_resume_that_cannot_take_the_lock(tmp_root, monkeypatch),
        fail={2: failure},
    )
    tab_drain.run(tmp_root)
    assert log == [(0, STALE, "")]
    failed = (sessions.get(tmp_root, SID) or {})[tab_reset.RESTORE_FAILED_FIELD]
    assert failed["label"] == f"✅ {DISPLAY}" and failed["reason"]


@needs_node
def test_the_whole_resume_stays_within_its_budget_and_a_reset_it_left_is_refused(tmp_root: Path, monkeypatch):
    """Both locks held: the reset lock's wait and the drainer lock's wait share one budget, so the
    resume returns within it in total. The withdrawal times out and leaves the reset queued; the
    drainer then refuses it, since the thread is resumed."""
    _ended_with_a_queued_reset(tmp_root, monkeypatch)
    monkeypatch.setattr(tab_reset, "RESUME_BUDGET_S", 0.3, raising=False)
    monkeypatch.setattr(tab_reset, "RESET_LOCK_WAIT_S", 0.4)  # longer than the whole budget
    record = sessions.get(tmp_root, SID) or {}
    worker = threading.Thread(target=activity_mod.mark_resumed, args=(tmp_root, SID, record), daemon=True)
    with tab_drain._locked(tmp_root), tab_reset.reset_lock(tmp_root, SID):
        started = time.monotonic()
        worker.start()
        worker.join(timeout=1.0)
        still_waiting = worker.is_alive()
        elapsed = time.monotonic() - started
    worker.join(timeout=5)
    assert not still_waiting and elapsed < 0.55
    assert (sessions.get(tmp_root, SID) or {})[tab_drain.RESUMED_FIELD] is True
    assert tab_drain.read_entry(tmp_root / tab_drain.TABS_DIR / f"manager-{SID}.json")["reset"] is True

    tabs = [STALE, OTHER]
    log = _fake_obsidian(monkeypatch, tabs)
    tab_drain.run(tmp_root)
    assert log == []
